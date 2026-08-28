"""Unit tests for RiskManager.calculate_order (position sizing, stop-loss/take-profit).

RiskManager.calculate_order is declared async but never awaits anything internally
(no DB/network access), so these tests just drive it with asyncio.run() instead of
pulling in pytest-asyncio as a dependency.
"""
import asyncio

from app.models.models import Strategy
from app.services.order_executor import OrderParams, RiskManager
from app.services.strategy_engine import Signal


def _strategy(**overrides) -> Strategy:
    defaults = dict(
        symbol="BTC/USDT",
        max_position_pct=10.0,
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
    )
    defaults.update(overrides)
    return Strategy(**defaults)


def _calculate(strategy: Strategy, signal: Signal, price: float, portfolio: float) -> OrderParams | None:
    return asyncio.run(RiskManager(strategy).calculate_order(signal, price, portfolio))


class TestHoldSignal:
    def test_hold_never_produces_an_order(self):
        order = _calculate(_strategy(), Signal.HOLD, price=100.0, portfolio=1000.0)
        assert order is None


class TestBuySizing:
    def test_position_size_and_levels(self):
        # 10% of a 1000 USDT portfolio at price 100 -> 100 USDT position -> 1.0 BTC.
        order = _calculate(_strategy(), Signal.BUY, price=100.0, portfolio=1000.0)
        assert order.side.value == "BUY"
        assert order.quantity == 1.0
        assert order.entry_price == 100.0
        # 2% below entry / 4% above entry.
        assert order.stop_loss == 98.0
        assert order.take_profit == 104.0

    def test_uses_the_strategy_symbol(self):
        order = _calculate(_strategy(symbol="ETH/USDT"), Signal.BUY, price=100.0, portfolio=1000.0)
        assert order.symbol == "ETH/USDT"


class TestSellSizing:
    def test_stop_and_take_profit_are_inverted_for_shorts(self):
        # For a SELL, stop-loss sits above entry and take-profit sits below it.
        order = _calculate(_strategy(), Signal.SELL, price=100.0, portfolio=1000.0)
        assert order.side.value == "SELL"
        assert order.stop_loss == 102.0
        assert order.take_profit == 96.0


class TestMinimumNotional:
    def test_position_below_10_usdt_is_rejected(self):
        # 1% of 500 USDT = 5 USDT, below the $10 Binance minimum notional -> no order.
        order = _calculate(_strategy(max_position_pct=1.0), Signal.BUY, price=100.0, portfolio=500.0)
        assert order is None

    def test_position_at_exactly_10_usdt_is_accepted(self):
        order = _calculate(_strategy(max_position_pct=10.0), Signal.BUY, price=100.0, portfolio=100.0)
        assert order is not None
