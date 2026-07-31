"""
Risk Manager — validates trade sizing and stop-loss before execution
Order Executor — places orders on Binance and persists trades to DB
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.models import Trade, OrderSide, OrderStatus, Strategy
from app.services.binance_service import BinanceService
from app.services.strategy_engine import Signal


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
        """Places or simulates a market order and persists the trade."""
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
        self.db.add(trade)
        await self.db.commit()
        await self.db.refresh(trade)
        return trade

    async def close_trade(self, trade: Trade, exit_price: float) -> Trade:
        """Closes an open trade and calculates P&L."""
        if not self.paper_trading:
            close_side = "sell" if trade.side == OrderSide.BUY else "buy"
            await self.binance.create_market_order(trade.symbol, close_side, trade.quantity)

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
