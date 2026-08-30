"""
Reconciler — syncs OPEN trades against Binance's real account state.

The bot's stop-loss/take-profit is just polling: it compares the trades table
against whatever candle it fetches on the next cycle. Nothing about that
notices a position that changed for a reason outside run_strategy_cycle — a
manual sell on Binance, a crash between opening a trade and the next cycle,
anything the bot itself didn't do. Left unnoticed, a stale OPEN row also blocks
the strategy from ever opening a new trade, since run_strategy_cycle only opens
one when there's no open trade already.

This runs once whenever a strategy (re)starts — see app/core/scheduler.py's
start_scheduler() and app/api/bot.py's start_bot() — before any cycle runs, so
the bot never trades on top of stale local state.

Only meaningful for real trading (paper_trading=False): paper trades have no
real Binance position to compare against.
"""
import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import BotConfig, OrderSide, OrderStatus, Strategy, StrategyRun, Trade
from app.services.binance_service import BinanceService

logger = logging.getLogger("bot_scheduler")

# Balances at or below this are "no position" — dust from fees/roundoff, not a
# real holding.
DUST_EPSILON = 1e-8

# How far the real balance can drift from the trade's recorded quantity (fees,
# roundoff) before it's a genuine mismatch instead of "matches".
MISMATCH_TOLERANCE_PCT = 1.0


async def reconcile_strategy(strategy: Strategy, bot_config: BotConfig, db: AsyncSession) -> dict:
    """Compares the strategy's real Binance balance against its OPEN trade, if
    any, and corrects or flags what it finds. Returns a small summary dict
    (mainly for tests/logging) — the real effect is on `db`: a stale trade gets
    closed, or a StrategyRun log row gets added, and both are committed here."""
    if bot_config.paper_trading:
        return {"status": "SKIPPED", "reason": "Paper trading — nothing to reconcile"}

    base_asset = strategy.symbol.split("/")[0]

    binance = BinanceService(
        api_key_enc=bot_config.binance_api_key_enc,
        secret_enc=bot_config.binance_secret_key_enc,
        testnet=bot_config.testnet,
        use_credentials=True,
    )
    try:
        balance = await binance.fetch_balance()
        real_qty = float(balance.get(base_asset, 0) or 0)

        open_trade = (await db.execute(
            select(Trade).where(Trade.strategy_id == strategy.id, Trade.status == OrderStatus.OPEN)
        )).scalar_one_or_none()

        if open_trade is None:
            if real_qty > DUST_EPSILON:
                await _log(db, strategy, "RECONCILE_MISMATCH",
                    f"Balance real de {real_qty} {base_asset} sin ningun trade OPEN "
                    "registrado -- posible posicion abierta por fuera del bot.",
                    {"symbol": strategy.symbol, "real_balance": real_qty})
                return {"status": "MISMATCH", "reason": "Untracked balance", "real_qty": real_qty}
            return {"status": "OK", "reason": "No open trade, no balance"}

        expected_qty = float(open_trade.quantity)

        if real_qty <= DUST_EPSILON:
            exit_price = await _find_exit_price(binance, strategy.symbol, open_trade)
            _close_stale_trade(open_trade, exit_price)
            await db.commit()
            await _log(db, strategy, "RECONCILED",
                f"Trade {open_trade.id} cerrado fuera del bot (balance real ~0 de "
                f"{base_asset}). Cerrado localmente a {exit_price}.",
                {"symbol": strategy.symbol, "expected_qty": expected_qty,
                 "real_qty": real_qty, "exit_price": exit_price})
            return {"status": "CLOSED", "trade_id": str(open_trade.id), "exit_price": exit_price}

        drift_pct = abs(real_qty - expected_qty) / expected_qty * 100 if expected_qty else 100.0
        if drift_pct > MISMATCH_TOLERANCE_PCT:
            await _log(db, strategy, "RECONCILE_MISMATCH",
                f"Trade {open_trade.id} espera {expected_qty} {base_asset} pero el "
                f"balance real es {real_qty} {base_asset} ({drift_pct:.1f}% de "
                "diferencia) -- revisar manualmente, no se toca automaticamente.",
                {"symbol": strategy.symbol, "expected_qty": expected_qty, "real_qty": real_qty})
            return {"status": "MISMATCH", "trade_id": str(open_trade.id),
                    "real_qty": real_qty, "expected_qty": expected_qty}

        return {"status": "OK", "trade_id": str(open_trade.id)}
    finally:
        await binance.close()


async def _find_exit_price(binance: BinanceService, symbol: str, trade: Trade) -> float:
    """Best-effort real exit price from Binance's own fill history since the
    trade was opened; falls back to the current ticker price if no matching
    fill shows up (e.g. account trade history doesn't reach back far enough)."""
    closing_side = "sell" if trade.side == OrderSide.BUY else "buy"
    try:
        fills = await binance.fetch_my_trades(symbol, since=trade.opened_at)
    except Exception:
        logger.warning(f"fetch_my_trades failed for {symbol}, falling back to ticker price", exc_info=True)
        fills = []

    matching = [f for f in fills if str(f.get("side", "")).lower() == closing_side]
    total_amount = sum(float(f["amount"]) for f in matching)
    if total_amount > 0:
        return sum(float(f["price"]) * float(f["amount"]) for f in matching) / total_amount

    ticker = await binance.fetch_ticker(symbol)
    return float(ticker["last"])


def _close_stale_trade(trade: Trade, exit_price: float) -> None:
    """Closes a trade locally to match a position that's already gone on
    Binance -- unlike OrderExecutor.close_trade, this never places an order:
    there's nothing left to sell/buy back."""
    pnl = (exit_price - trade.entry_price) * trade.quantity
    if trade.side == OrderSide.SELL:
        pnl = -pnl

    trade.exit_price = exit_price
    trade.pnl        = round(pnl, 4)
    trade.pnl_pct    = round((pnl / (trade.entry_price * trade.quantity)) * 100, 2)
    trade.status     = OrderStatus.FILLED
    trade.closed_at  = datetime.utcnow()


async def _log(db: AsyncSession, strategy: Strategy, signal: str, reason: str, indicators: dict) -> None:
    db.add(StrategyRun(
        strategy_id=strategy.id,
        signal=signal,
        reason=reason,
        indicators=json.dumps(indicators),
        price=None,
    ))
    await db.commit()
