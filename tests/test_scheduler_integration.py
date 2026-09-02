"""Integration tests for run_strategy_cycle: the real DB (an isolated Postgres
schema, see conftest.py) plus a fake Binance, driving the full cycle end to end —
fetch candles, persist them, run the strategy, size and open/close trades.

Requires a reachable Postgres (docker-compose up -d db, or set TEST_DATABASE_URL).
Run with: pytest -m integration
Everything here is marked `integration` so the plain `pytest -q` unit run stays
fast and dependency-free.
"""
import json
import uuid
from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import select

from app.core import scheduler
from app.models.models import (
    BotConfig,
    BotStatus,
    OrderSide,
    OrderStatus,
    PriceCandle,
    Strategy,
    StrategyRun,
    Trade,
    User,
)

pytestmark = pytest.mark.integration


# ── Test doubles ─────────────────────────────────────────────────────────────────

class FakeBinanceService:
    """Stands in for BinanceService: no network calls, canned responses, records
    every order it's asked to place so tests can assert on them."""

    def __init__(
        self,
        ohlcv_df: pd.DataFrame = None,
        *,
        ticker_price: float = None,
        balance_usdt: float = 1000.0,
        fail_stop_loss_placement: bool = False,
        simulate_cancel_race: bool = False,
    ):
        self.ohlcv_df = ohlcv_df
        self.ticker_price = ticker_price
        self.balance_usdt = balance_usdt
        self.orders: list[dict] = []
        self.stop_orders: dict[str, dict] = {}
        self.cancelled_orders: list[str] = []
        self.fail_stop_loss_placement = fail_stop_loss_placement
        # Simulates the exchange filling a stop-loss order in the exact window
        # between us checking its status and us trying to cancel it.
        self.simulate_cancel_race = simulate_cancel_race
        self.closed = False
        self._next_stop_id = 1

    async def fetch_ohlcv(self, symbol, timeframe, limit=200):
        return self.ohlcv_df

    async def fetch_ticker(self, symbol):
        return {"last": self.ticker_price}

    async def fetch_balance(self):
        return {"USDT": self.balance_usdt}

    async def create_market_order(self, symbol, side, amount):
        order = {"id": f"fake-{len(self.orders) + 1}", "symbol": symbol, "side": side, "amount": amount}
        self.orders.append(order)
        return order

    async def create_stop_loss_order(self, symbol, side, amount, stop_price, **kwargs):
        if self.fail_stop_loss_placement:
            raise ConnectionError("Binance rejected the stop-loss order")
        order_id = f"stop-{self._next_stop_id}"
        self._next_stop_id += 1
        self.stop_orders[order_id] = {
            "id": order_id, "symbol": symbol, "status": "open",
            "price": stop_price, "average": stop_price,
        }
        return self.stop_orders[order_id]

    async def fetch_order(self, order_id, symbol):
        return self.stop_orders[order_id]

    async def cancel_order(self, order_id, symbol):
        order = self.stop_orders.get(order_id)
        if order and self.simulate_cancel_race:
            order["status"] = "closed"
            raise Exception("Order already filled, cannot cancel")
        self.cancelled_orders.append(order_id)
        if order:
            order["status"] = "canceled"
        return order

    async def close(self):
        self.closed = True

    def mark_stop_order_filled(self, order_id: str, fill_price: float | None = None):
        """Test helper: simulates Binance having executed a resting stop-loss
        order on its own, between cycles."""
        order = self.stop_orders[order_id]
        order["status"] = "closed"
        if fill_price is not None:
            order["average"] = fill_price


def _ohlcv_df(closes: list[float], *, spike_low: float | None = None, spike_high: float | None = None) -> pd.DataFrame:
    """Builds a minimal OHLCV frame from a close series. high/low default to a
    tight band around close; the last candle's high/low can be overridden to
    simulate an intra-candle wick through a stop-loss/take-profit level without
    disturbing the closes that drive the strategy's indicators."""
    n = len(closes)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="D"),
        "open":   closes,
        "high":   [c * 1.001 for c in closes],
        "low":    [c * 0.999 for c in closes],
        "close":  closes,
        "volume": [1000.0] * n,
    })
    if spike_high is not None:
        df.loc[df.index[-1], "high"] = spike_high
    if spike_low is not None:
        df.loc[df.index[-1], "low"] = spike_low
    return df


def _patch_binance(monkeypatch, fake: FakeBinanceService):
    monkeypatch.setattr(scheduler, "BinanceService", lambda **kwargs: fake)


def _patch_session(monkeypatch, session_factory):
    monkeypatch.setattr(scheduler, "AsyncSessionLocal", session_factory)


# Golden cross with fast=3/slow=5, no trend filter — same shape verified in
# test_strategy_engine.py: fast SMA crosses above slow SMA on the last candle.
GOLDEN_CROSS_CLOSES = [50, 49, 48, 47, 46, 45, 44, 43, 42, 60]
FLAT_CLOSES = [100.0] * 6  # no crossover for fast=3/slow=5 -> HOLD


# ── Helper: a runnable strategy ────────────────────────────────────────────────────

async def _make_runnable_strategy(
    db_session, *, params: dict, symbol="BTC/USDT", paper_trading: bool = True
) -> Strategy:
    user = User(email=f"{uuid.uuid4()}@test.local", password_hash="x")
    db_session.add(user)
    await db_session.flush()

    strategy = Strategy(
        user_id=user.id,
        name="test-strategy",
        type="sma_crossover",
        symbol=symbol,
        timeframe="1d",
        params=json.dumps(params),
        max_position_pct=10.0,
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
        is_active=True,
    )
    db_session.add(strategy)
    await db_session.flush()

    bot_config = BotConfig(
        user_id=user.id,
        strategy_id=strategy.id,
        binance_api_key_enc="enc-key",
        binance_secret_key_enc="enc-secret",
        testnet=True,
        paper_trading=paper_trading,
        status=BotStatus.RUNNING,
    )
    db_session.add(bot_config)
    await db_session.commit()

    strategy.bot_config = bot_config
    return strategy


# ── Tests ─────────────────────────────────────────────────────────────────────────

class TestOpeningATrade:
    async def test_buy_signal_opens_a_trade(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}
        )
        fake = FakeBinanceService(_ohlcv_df(GOLDEN_CROSS_CLOSES), ticker_price=60.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result["status"] == "COMPLETED"
        assert result["signal"] == "BUY"
        assert result["trade_id"] is not None

        trade = (await db_session.execute(
            select(Trade).where(Trade.strategy_id == strategy.id)
        )).scalar_one()
        assert trade.side == OrderSide.BUY
        assert trade.status == OrderStatus.OPEN
        assert trade.entry_price == 60.0
        assert trade.stop_loss == pytest.approx(58.8)
        assert trade.take_profit == pytest.approx(62.4)

        run = (await db_session.execute(
            select(StrategyRun).where(StrategyRun.strategy_id == strategy.id)
        )).scalar_one()
        assert run.signal == "BUY"

        candle = (await db_session.execute(
            select(PriceCandle).where(PriceCandle.symbol == strategy.symbol)
        )).scalar_one()
        assert candle.close == 60.0

    async def test_hold_signal_opens_no_trade(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}
        )
        fake = FakeBinanceService(_ohlcv_df(FLAT_CLOSES), ticker_price=100.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result["status"] == "COMPLETED"
        assert result["signal"] == "HOLD"
        assert result["trade_id"] is None

        trades = (await db_session.execute(
            select(Trade).where(Trade.strategy_id == strategy.id)
        )).scalars().all()
        assert trades == []


class TestClosingATrade:
    async def _open_trade(self, db_session, strategy, *, stop_loss=95.0, take_profit=110.0) -> Trade:
        trade = Trade(
            strategy_id=strategy.id,
            symbol=strategy.symbol,
            side=OrderSide.BUY,
            status=OrderStatus.OPEN,
            entry_price=100.0,
            quantity=1.0,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=datetime.utcnow(),
        )
        db_session.add(trade)
        await db_session.commit()
        return trade

    async def test_take_profit_wick_closes_the_trade(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}
        )
        open_trade = await self._open_trade(db_session, strategy, stop_loss=95.0, take_profit=110.0)

        # Closes stay flat (HOLD) but the candle's high wicks above take_profit.
        fake = FakeBinanceService(_ohlcv_df(FLAT_CLOSES, spike_high=115.0), ticker_price=100.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result["status"] == "COMPLETED"
        assert result["reason"] == "Take-profit hit"

        await db_session.refresh(open_trade)
        assert open_trade.status == OrderStatus.FILLED
        assert open_trade.exit_price == 110.0
        assert open_trade.pnl == pytest.approx(10.0)
        assert open_trade.closed_at is not None

    async def test_stop_loss_wick_closes_the_trade_at_a_loss(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}
        )
        open_trade = await self._open_trade(db_session, strategy, stop_loss=95.0, take_profit=110.0)

        fake = FakeBinanceService(_ohlcv_df(FLAT_CLOSES, spike_low=90.0), ticker_price=100.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result["reason"] == "Stop-loss hit"
        await db_session.refresh(open_trade)
        assert open_trade.status == OrderStatus.FILLED
        assert open_trade.exit_price == 95.0
        assert open_trade.pnl == pytest.approx(-5.0)


class TestRealStopLossOrder:
    """paper_trading=False: the stop-loss becomes a real order on Binance
    instead of pure candle-wick polling. See app/services/order_executor.py
    and app/services/reconciler.py."""

    async def test_buy_opens_a_trade_with_a_real_stop_loss_order(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}, paper_trading=False
        )
        fake = FakeBinanceService(_ohlcv_df(GOLDEN_CROSS_CLOSES), ticker_price=60.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)

        result = await scheduler.run_strategy_cycle(str(strategy.id))
        assert result["status"] == "COMPLETED"

        trade = (await db_session.execute(select(Trade).where(Trade.strategy_id == strategy.id))).scalar_one()
        assert trade.stop_loss_order_id is not None
        stop_order = fake.stop_orders[trade.stop_loss_order_id]
        assert stop_order["price"] == pytest.approx(trade.stop_loss)
        # A market sell for the entry, nothing else placed yet.
        assert len(fake.orders) == 1

    async def test_stop_loss_placement_failure_falls_back_to_wick_polling(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}, paper_trading=False
        )
        fake = FakeBinanceService(_ohlcv_df(GOLDEN_CROSS_CLOSES), ticker_price=60.0, fail_stop_loss_placement=True)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)

        result = await scheduler.run_strategy_cycle(str(strategy.id))
        assert result["status"] == "COMPLETED"

        trade = (await db_session.execute(select(Trade).where(Trade.strategy_id == strategy.id))).scalar_one()
        assert trade.stop_loss_order_id is None

        # Next cycle: no real stop order to check, so the old wick check must
        # still catch it -- same as before this feature existed.
        fake2 = FakeBinanceService(_ohlcv_df(FLAT_CLOSES, spike_low=trade.stop_loss - 1), ticker_price=trade.stop_loss)
        _patch_binance(monkeypatch, fake2)

        result = await scheduler.run_strategy_cycle(str(strategy.id))
        assert result["reason"] == "Stop-loss hit"
        await db_session.refresh(trade)
        assert trade.status == OrderStatus.FILLED

    async def test_stop_loss_order_filled_on_exchange_closes_trade_without_a_new_order(
        self, db_session, session_factory, monkeypatch
    ):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}, paper_trading=False
        )
        fake = FakeBinanceService(_ohlcv_df(GOLDEN_CROSS_CLOSES), ticker_price=60.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)
        await scheduler.run_strategy_cycle(str(strategy.id))

        trade = (await db_session.execute(select(Trade).where(Trade.strategy_id == strategy.id))).scalar_one()
        stop_order_id = trade.stop_loss_order_id
        orders_before = len(fake.orders)

        # Binance filled the resting stop order on its own between cycles.
        fake.mark_stop_order_filled(stop_order_id, fill_price=57.5)
        fake.ohlcv_df = _ohlcv_df(FLAT_CLOSES)  # no wick-based trigger this cycle

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result["status"] == "COMPLETED"
        assert result["reason"] == "Stop-loss hit (orden real en Binance)"
        assert result["indicators"]["exit_price"] == pytest.approx(57.5)
        assert len(fake.orders) == orders_before  # no new market order placed

        await db_session.refresh(trade)
        assert trade.status == OrderStatus.FILLED
        assert trade.exit_price == pytest.approx(57.5)

    async def test_take_profit_cancels_the_outstanding_stop_loss_order(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}, paper_trading=False
        )
        fake = FakeBinanceService(_ohlcv_df(GOLDEN_CROSS_CLOSES), ticker_price=60.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)
        await scheduler.run_strategy_cycle(str(strategy.id))

        trade = (await db_session.execute(select(Trade).where(Trade.strategy_id == strategy.id))).scalar_one()
        stop_order_id = trade.stop_loss_order_id

        fake.ohlcv_df = _ohlcv_df(FLAT_CLOSES, spike_high=trade.take_profit + 5)

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result["reason"] == "Take-profit hit"
        assert stop_order_id in fake.cancelled_orders
        await db_session.refresh(trade)
        assert trade.status == OrderStatus.FILLED
        assert trade.exit_price == pytest.approx(trade.take_profit)

    async def test_cancel_race_uses_the_real_fill_instead_of_selling_again(self, db_session, session_factory, monkeypatch):
        """The stop-loss order fills on Binance right as we try to cancel it
        (e.g. to close on take-profit) -- the cancel fails, and we must use
        the order's real fill price instead of placing a second sell."""
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}, paper_trading=False
        )
        fake = FakeBinanceService(_ohlcv_df(GOLDEN_CROSS_CLOSES), ticker_price=60.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)
        await scheduler.run_strategy_cycle(str(strategy.id))

        trade = (await db_session.execute(select(Trade).where(Trade.strategy_id == strategy.id))).scalar_one()
        orders_before = len(fake.orders)

        fake.simulate_cancel_race = True
        fake.stop_orders[trade.stop_loss_order_id]["average"] = 55.0
        fake.ohlcv_df = _ohlcv_df(FLAT_CLOSES, spike_high=trade.take_profit + 5)

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result["status"] == "COMPLETED"
        assert result["indicators"]["exit_price"] == pytest.approx(55.0)
        assert len(fake.orders) == orders_before  # no second market sell placed
        await db_session.refresh(trade)
        assert trade.exit_price == pytest.approx(55.0)


class TestGatingAndErrors:
    async def test_paused_bot_is_skipped(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}
        )
        strategy.bot_config.status = BotStatus.PAUSED
        await db_session.commit()

        fake = FakeBinanceService(_ohlcv_df(GOLDEN_CROSS_CLOSES), ticker_price=60.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result == {"status": "SKIPPED", "reason": "Bot is not running"}
        assert fake.orders == []

    async def test_binance_failure_is_recorded_without_raising(self, db_session, session_factory, monkeypatch):
        strategy = await _make_runnable_strategy(
            db_session, params={"fast": 3, "slow": 5, "trend_filter_enabled": False}
        )

        class BrokenBinance(FakeBinanceService):
            async def fetch_ohlcv(self, symbol, timeframe, limit=200):
                raise ConnectionError("exchangeInfo unreachable")

        fake = BrokenBinance(_ohlcv_df(FLAT_CLOSES), ticker_price=100.0)
        _patch_binance(monkeypatch, fake)
        _patch_session(monkeypatch, session_factory)

        result = await scheduler.run_strategy_cycle(str(strategy.id))

        assert result["status"] == "FAILED"
        assert "exchangeInfo unreachable" in result["reason"]

        run = (await db_session.execute(
            select(StrategyRun).where(StrategyRun.strategy_id == strategy.id)
        )).scalar_one()
        assert run.signal == "FAILED"
