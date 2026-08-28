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

    def __init__(self, ohlcv_df: pd.DataFrame, *, ticker_price: float, balance_usdt: float = 1000.0):
        self.ohlcv_df = ohlcv_df
        self.ticker_price = ticker_price
        self.balance_usdt = balance_usdt
        self.orders: list[dict] = []
        self.closed = False

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

    async def close(self):
        self.closed = True


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

async def _make_runnable_strategy(db_session, *, params: dict, symbol="BTC/USDT") -> Strategy:
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
        paper_trading=True,
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
