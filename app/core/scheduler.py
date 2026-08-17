"""
Bot Scheduler — APScheduler-driven trading loop
Runs each active strategy on its configured timeframe
"""
import logging
import json
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.database import AsyncSessionLocal
from app.models.models import Strategy, BotConfig, BotStatus, Trade, OrderStatus, OrderSide, PriceCandle, StrategyRun
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


def _build_strategy_run(strategy: Strategy, result_sig, fallback_price: float | None = None) -> StrategyRun:
    indicators = result_sig.indicators or {}
    price = indicators.get("price", fallback_price)
    return StrategyRun(
        strategy_id=strategy.id,
        signal=result_sig.signal.value if hasattr(result_sig.signal, "value") else str(result_sig.signal),
        reason=result_sig.reason,
        indicators=json.dumps(indicators),
        price=price,
    )


def _build_failed_strategy_run(strategy: Strategy, reason: str) -> StrategyRun:
    return StrategyRun(
        strategy_id=strategy.id,
        signal="FAILED",
        reason=reason,
        indicators=json.dumps({
            "symbol": strategy.symbol,
            "timeframe": strategy.timeframe,
            "strategy_type": strategy.type,
            "testnet": strategy.bot_config.testnet,
            "paper_trading": strategy.bot_config.paper_trading,
        }),
        price=None,
    )


def _format_strategy_error(exc: Exception, strategy: Strategy) -> str:
    reason = f"{exc.__class__.__name__}: {exc}"
    if "exchangeInfo" in str(exc):
        host = (
            "testnet.binance.vision"
            if "testnet.binance.vision" in str(exc)
            else "api.binance.com"
        )
        mode = "Testnet" if strategy.bot_config.testnet else "Live"
        return (
            f"{reason}. Binance {mode} market metadata is not reachable from this "
            f"environment ({host}). Check DNS, firewall, VPN/proxy, antivirus web "
            "protection, ISP or regional access to Binance endpoints."
        )
    return reason


def _check_stop_take_exit(open_trade: Trade, last_candle) -> tuple[float | None, str | None]:
    """Checks whether the candle's high/low triggered the trade's stop-loss or take-profit.

    Stop-loss is checked before take-profit: if a single candle's range spans both
    levels, we don't know the intra-candle order, so we assume the worse outcome.
    """
    low  = float(last_candle["low"])
    high = float(last_candle["high"])

    if open_trade.side == OrderSide.BUY:
        if open_trade.stop_loss is not None and low <= float(open_trade.stop_loss):
            return float(open_trade.stop_loss), "Stop-loss hit"
        if open_trade.take_profit is not None and high >= float(open_trade.take_profit):
            return float(open_trade.take_profit), "Take-profit hit"
    else:  # SELL (short)
        if open_trade.stop_loss is not None and high >= float(open_trade.stop_loss):
            return float(open_trade.stop_loss), "Stop-loss hit"
        if open_trade.take_profit is not None and low <= float(open_trade.take_profit):
            return float(open_trade.take_profit), "Take-profit hit"

    return None, None


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
            db.add(_build_strategy_run(strategy, result_sig, float(last["close"])))
            logger.info(f"[{strategy.symbol}] {strategy.type}: {result_sig.signal} — {result_sig.reason}")

            open_trade_result = await db.execute(
                select(Trade).where(
                    Trade.strategy_id == strategy.id,
                    Trade.status      == OrderStatus.OPEN,
                )
            )
            open_trade = open_trade_result.scalar_one_or_none()
            executor = OrderExecutor(
                binance,
                db,
                paper_trading=strategy.bot_config.paper_trading,
            )

            if open_trade:
                close_price, close_reason = _check_stop_take_exit(open_trade, last)

                if close_price is None and result_sig.signal == Signal.SELL:
                    close_price = float(last["close"])
                    close_reason = result_sig.reason

                if close_price is not None:
                    trade = await executor.close_trade(open_trade, close_price)
                    logger.info(f"[{strategy.symbol}] Trade closed: {trade.id} @ {trade.exit_price}")
                    return {
                        "status": "COMPLETED",
                        "signal": Signal.SELL,
                        "reason": close_reason,
                        "indicators": {
                            **result_sig.indicators,
                            "exit_price": close_price,
                            "exit_reason": close_reason,
                        },
                        "trade_id": str(trade.id),
                    }

                await db.commit()
                return {
                    "status": "COMPLETED",
                    "signal": result_sig.signal,
                    "reason": "Trade already open",
                    "indicators": result_sig.indicators,
                    "trade_id": str(open_trade.id),
                }

            if result_sig.signal == Signal.HOLD:
                await db.commit()
                return {
                    "status": "COMPLETED",
                    "signal": result_sig.signal,
                    "reason": result_sig.reason,
                    "indicators": result_sig.indicators,
                    "trade_id": None,
                }

            if result_sig.signal == Signal.SELL:
                await db.commit()
                return {
                    "status": "COMPLETED",
                    "signal": result_sig.signal,
                    "reason": "Sell signal ignored because no long position is open",
                    "indicators": result_sig.indicators,
                    "trade_id": None,
                }

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
            reason = _format_strategy_error(e, strategy)
            logger.error(f"Strategy cycle error [{strategy_id}]: {reason}", exc_info=True)
            db.add(_build_failed_strategy_run(strategy, reason))
            await db.commit()
            return {"status": "FAILED", "reason": reason}
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
