"""Unit tests for the stop-loss / take-profit exit check used by the trading loop."""
import pytest

from app.core.scheduler import _check_stop_take_exit
from app.models.models import OrderSide, Trade


def _candle(low: float, high: float) -> dict:
    return {"low": low, "high": high}


def _trade(side: OrderSide, stop_loss: float | None, take_profit: float | None) -> Trade:
    return Trade(side=side, stop_loss=stop_loss, take_profit=take_profit)


class TestLongTrade:
    def test_stop_loss_hit(self):
        trade = _trade(OrderSide.BUY, stop_loss=90, take_profit=110)
        price, reason = _check_stop_take_exit(trade, _candle(low=85, high=95))
        assert (price, reason) == (90, "Stop-loss hit")

    def test_take_profit_hit(self):
        trade = _trade(OrderSide.BUY, stop_loss=90, take_profit=110)
        price, reason = _check_stop_take_exit(trade, _candle(low=100, high=115))
        assert (price, reason) == (110, "Take-profit hit")

    def test_no_exit_within_range(self):
        trade = _trade(OrderSide.BUY, stop_loss=90, take_profit=110)
        price, reason = _check_stop_take_exit(trade, _candle(low=95, high=105))
        assert (price, reason) == (None, None)

    def test_both_hit_in_same_candle_prioritizes_stop_loss(self):
        trade = _trade(OrderSide.BUY, stop_loss=90, take_profit=110)
        price, reason = _check_stop_take_exit(trade, _candle(low=85, high=115))
        assert (price, reason) == (90, "Stop-loss hit")


class TestShortTrade:
    def test_stop_loss_hit(self):
        trade = _trade(OrderSide.SELL, stop_loss=110, take_profit=90)
        price, reason = _check_stop_take_exit(trade, _candle(low=95, high=115))
        assert (price, reason) == (110, "Stop-loss hit")

    def test_take_profit_hit(self):
        trade = _trade(OrderSide.SELL, stop_loss=110, take_profit=90)
        price, reason = _check_stop_take_exit(trade, _candle(low=85, high=100))
        assert (price, reason) == (90, "Take-profit hit")

    def test_no_exit_within_range(self):
        trade = _trade(OrderSide.SELL, stop_loss=110, take_profit=90)
        price, reason = _check_stop_take_exit(trade, _candle(low=95, high=105))
        assert (price, reason) == (None, None)

    def test_both_hit_in_same_candle_prioritizes_stop_loss(self):
        trade = _trade(OrderSide.SELL, stop_loss=110, take_profit=90)
        price, reason = _check_stop_take_exit(trade, _candle(low=85, high=115))
        assert (price, reason) == (110, "Stop-loss hit")


def test_no_levels_configured_never_exits():
    trade = _trade(OrderSide.BUY, stop_loss=None, take_profit=None)
    price, reason = _check_stop_take_exit(trade, _candle(low=0, high=1_000_000))
    assert (price, reason) == (None, None)
