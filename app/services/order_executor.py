"""
Risk Manager — validates trade sizing and stop-loss before execution
Order Executor — places orders on Binance and persists trades to DB
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.models import Trade, OrderSide, OrderStatus, Strategy
from app.services.binance_service import BinanceService
from app.services.strategy_engine import Signal

logger = logging.getLogger("bot_scheduler")


# ── Risk Manager ──────────────────────────────────────────────────────────────

@dataclass
class OrderParams:
    symbol:      str
    side:        OrderSide
    quantity:    float
    entry_price: float
    stop_loss:   float
    take_profit: float


class RiskManager:
    def __init__(self, strategy: Strategy):
        self.strategy = strategy

    async def calculate_order(
        self,
        signal: Signal,
        current_price: float,
        portfolio_usdt: float,
    ) -> Optional[OrderParams]:
        """
        Returns OrderParams if risk checks pass, else None.
        """
        if signal == Signal.HOLD:
            return None

        # Position size in USDT
        position_usdt = portfolio_usdt * (self.strategy.max_position_pct / 100)
        quantity      = position_usdt / current_price

        if signal == Signal.BUY:
            stop_loss   = current_price * (1 - self.strategy.stop_loss_pct   / 100)
            take_profit = current_price * (1 + self.strategy.take_profit_pct / 100)
            side        = OrderSide.BUY
        else:  # SELL
            stop_loss   = current_price * (1 + self.strategy.stop_loss_pct   / 100)
            take_profit = current_price * (1 - self.strategy.take_profit_pct / 100)
            side        = OrderSide.SELL

        # Minimum notional: most Binance pairs require > $10
        if position_usdt < 10:
            return None

        return OrderParams(
            symbol      = self.strategy.symbol,
            side        = side,
            quantity    = round(quantity, 6),
            entry_price = current_price,
            stop_loss   = round(stop_loss, 4),
            take_profit = round(take_profit, 4),
        )


# ── Order Executor ────────────────────────────────────────────────────────────

class OrderExecutor:
    def __init__(
        self,
        binance: BinanceService,
        db: AsyncSession,
        paper_trading: bool = False,
    ):
        self.binance       = binance
        self.db            = db
        self.paper_trading = paper_trading

    async def execute(self, strategy: Strategy, order: OrderParams) -> Trade:
        """Places or simulates a market order and persists the trade. For real
        trading, also places a real STOP_LOSS_LIMIT order on Binance so the
        stop-loss is enforced by the exchange, not just by our own polling --
        see app/core/scheduler.py for how the two paths interact.

        If placing that protective order fails (rejected, network error), the
        trade still opens: it just falls back to the old polling-only
        behaviour for its stop-loss, same as before this existed. Better a
        trade the bot has to poll for than one it refuses to open at all."""
        if self.paper_trading:
            exchange_order_id = f"paper-{uuid.uuid4()}"
        else:
            raw = await self.binance.create_market_order(
                symbol = order.symbol,
                side   = order.side.value.lower(),
                amount = order.quantity,
            )
            exchange_order_id = str(raw.get("id"))

        trade = Trade(
            strategy_id       = strategy.id,
            exchange_order_id = exchange_order_id,
            symbol            = order.symbol,
            side              = order.side,
            status            = OrderStatus.OPEN,
            entry_price       = order.entry_price,
            quantity          = order.quantity,
            stop_loss         = order.stop_loss,
            take_profit       = order.take_profit,
            opened_at         = datetime.utcnow(),
        )

        if not self.paper_trading:
            close_side = "sell" if order.side == OrderSide.BUY else "buy"
            try:
                stop_order = await self.binance.create_stop_loss_order(
                    order.symbol, close_side, order.quantity, order.stop_loss,
                )
                trade.stop_loss_order_id = str(stop_order.get("id"))
            except Exception:
                logger.error(
                    f"Could not place real stop-loss order for {order.symbol} -- "
                    "falling back to polling for this trade's stop-loss.",
                    exc_info=True,
                )

        self.db.add(trade)
        await self.db.commit()
        await self.db.refresh(trade)
        return trade

    async def close_trade(self, trade: Trade, exit_price: float) -> Trade:
        """Closes a trade for take-profit or a strategy exit signal -- i.e. any
        close that isn't the real stop-loss order itself firing (see
        close_trade_from_stop_loss_fill for that path)."""
        if not self.paper_trading:
            race_fill_price = await self._cancel_stop_loss_order(trade)
            if race_fill_price is not None:
                # The stop-loss order won the race against our cancel -- it
                # already sold the position for real. Use its real price
                # instead of placing a second sell on a balance that's gone.
                exit_price = race_fill_price
            else:
                close_side = "sell" if trade.side == OrderSide.BUY else "buy"
                await self.binance.create_market_order(trade.symbol, close_side, trade.quantity)

        return await self._finalize_close(trade, exit_price)

    async def close_trade_from_stop_loss_fill(self, trade: Trade) -> Trade:
        """Closes a trade whose real stop-loss order already executed on
        Binance -- no new order to place, just record what really happened."""
        stop_order = await self.binance.fetch_order(trade.stop_loss_order_id, trade.symbol)
        exit_price = float(stop_order.get("average") or stop_order.get("price") or 0)
        return await self._finalize_close(trade, exit_price)

    async def _cancel_stop_loss_order(self, trade: Trade) -> Optional[float]:
        """Cancels the trade's outstanding real stop-loss order, if any.
        Returns the real fill price if it turns out the order had already
        executed by the time we tried to cancel it (a race with the exchange),
        or None if there was nothing to cancel or it cancelled cleanly."""
        if not trade.stop_loss_order_id:
            return None
        try:
            await self.binance.cancel_order(trade.stop_loss_order_id, trade.symbol)
            return None
        except Exception:
            stop_order = await self.binance.fetch_order(trade.stop_loss_order_id, trade.symbol)
            if str(stop_order.get("status", "")).lower() in ("closed", "filled"):
                return float(stop_order.get("average") or stop_order.get("price") or 0)
            logger.warning(
                f"Could not cancel stop-loss order {trade.stop_loss_order_id} for "
                f"{trade.symbol} and it isn't filled either -- it may be left resting.",
                exc_info=True,
            )
            return None

    async def _finalize_close(self, trade: Trade, exit_price: float) -> Trade:
        pnl = (exit_price - trade.entry_price) * trade.quantity
        if trade.side == OrderSide.SELL:
            pnl = -pnl

        trade.exit_price = exit_price
        trade.pnl        = round(pnl, 4)
        trade.pnl_pct    = round((pnl / (trade.entry_price * trade.quantity)) * 100, 2)
        trade.status     = OrderStatus.FILLED
        trade.closed_at  = datetime.utcnow()

        await self.db.commit()
        await self.db.refresh(trade)
        return trade
