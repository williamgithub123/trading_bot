"""
Bot Scheduler — APScheduler-driven trading loop
Runs each active strategy on its configured timeframe
"""
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal
from app.models.models import Strategy, BotConfig, BotStatus, Trade, OrderStatus, PriceCandle
from app.services.binance_service import BinanceService
from app.services.strategy_engine import run_strategy, Signal
from app.services.order_executor import RiskManager, OrderExecutor

logger    = logging.getLogger("bot_scheduler")
scheduler = AsyncIOScheduler()

# Maps ccxt timeframe strings → cron-style minutes for APScheduler
TIMEFRAME_CRON = {
    "1m":  "* * * * *",
    "5m":  "*/5 * * * *",
    "15m": "*/15 * * * *",
    "1h":  "0 * * * *",
    "4h":  "0 */4 * * *",
    "1d":  "0 0 * * *",
}


async def run_strategy_cycle(strategy_id: str, force: bool = False):
    """Core trading loop — called by APScheduler for each active strategy."""
    async with AsyncSessionLocal() as db:
        filters = [Strategy.id == strategy_id]
        if not force:
            filters.append(Strategy.is_active == True)

        result = await db.execute(
            select(Strategy)
            .options(selectinload(Strategy.bot_config))
            .where(*filters)
        )
        strategy = result.scalar_one_or_none()
        if not strategy or not strategy.bot_config:
            return {"status": "SKIPPED", "reason": "Strategy or bot config not found"}
        if not force and strategy.bot_config.status != BotStatus.RUNNING:
            return {"status": "SKIPPED", "reason": "Bot is not running"}

        binance = BinanceService(
            api_key_enc = strategy.bot_config.binance_api_key_enc,
            secret_enc  = strategy.bot_config.binance_secret_key_enc,
            testnet     = strategy.bot_config.testnet,
            use_credentials = not strategy.bot_config.paper_trading,
        )
        try:
            # 1. Fetch market data
            df = await binance.fetch_ohlcv(strategy.symbol, strategy.timeframe)

            # 2. Persist latest candle
            last = df.iloc[-1]
            candle = PriceCandle(
                symbol    = strategy.symbol,
                timeframe = strategy.timeframe,
                timestamp = last["timestamp"],
                open=last["open"], high=last["high"],
                low=last["low"],   close=last["close"],
                volume=last["volume"],
            )
            db.add(candle)

            # 3. Run strategy
            result_sig = run_strategy(strategy.type, df, strategy.params)
            logger.info(f"[{strategy.symbol}] {strategy.type}: {result_sig.signal} — {result_sig.reason}")

            if result_sig.signal == Signal.HOLD:
                await db.commit()
                return {
                    "status": "COMPLETED",
                    "signal": result_sig.signal,
                    "reason": result_sig.reason,
                    "indicators": result_sig.indicators,
                    "trade_id": None,
                }

            # 4. Check for open trade — avoid double entry
            open_trade = await db.execute(
                select(Trade).where(
                    Trade.strategy_id == strategy.id,
                    Trade.status      == OrderStatus.OPEN,
                )
            )
            if open_trade.scalar_one_or_none():
                logger.info(f"[{strategy.symbol}] Trade already open, skipping.")
                await db.commit()
                return {
                    "status": "SKIPPED",
                    "signal": result_sig.signal,
                    "reason": "Trade already open",
                    "indicators": result_sig.indicators,
                    "trade_id": None,
                }

            # 5. Risk check
            if strategy.bot_config.paper_trading:
                usdt_avail = 1000
            else:
                balance    = await binance.fetch_balance()
                usdt_avail = balance.get("USDT", 0)
            ticker     = await binance.fetch_ticker(strategy.symbol)
            price      = ticker["last"]

            risk    = RiskManager(strategy)
            order   = await risk.calculate_order(result_sig.signal, price, usdt_avail)
            if not order:
                logger.warning(f"[{strategy.symbol}] Risk check failed, order skipped.")
                await db.commit()
                return {
                    "status": "SKIPPED",
                    "signal": result_sig.signal,
                    "reason": "Risk check failed",
                    "indicators": result_sig.indicators,
                    "trade_id": None,
                }

            # 6. Execute order
            executor = OrderExecutor(
                binance,
                db,
                paper_trading=strategy.bot_config.paper_trading,
            )
            trade = await executor.execute(strategy, order)
            logger.info(f"[{strategy.symbol}] Trade executed: {trade.id} @ {trade.entry_price}")
            return {
                "status": "COMPLETED",
                "signal": result_sig.signal,
                "reason": result_sig.reason,
                "indicators": result_sig.indicators,
                "trade_id": str(trade.id),
            }

        except Exception as e:
            logger.error(f"Strategy cycle error [{strategy_id}]: {e}", exc_info=True)
            return {"status": "FAILED", "reason": str(e)}
        finally:
            await binance.close()


async def schedule_strategy(strategy_id: str, timeframe: str):
    cron = TIMEFRAME_CRON.get(timeframe, "*/5 * * * *")
    scheduler.add_job(
        run_strategy_cycle,
        CronTrigger.from_crontab(cron),
        args=[strategy_id],
        id=str(strategy_id),
        replace_existing=True,
    )
    logger.info(f"Scheduled strategy {strategy_id} @ {cron}")


async def unschedule_strategy(strategy_id: str):
    try:
        scheduler.remove_job(str(strategy_id))
    except Exception:
        pass


async def start_scheduler():
    scheduler.start()
    # Resume any strategies that were running before restart
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Strategy).where(Strategy.is_active == True)
        )
        for strategy in result.scalars().all():
            await schedule_strategy(str(strategy.id), strategy.timeframe)


async def stop_scheduler():
    scheduler.shutdown(wait=False)
