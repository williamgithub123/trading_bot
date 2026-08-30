"""Integration tests for reconcile_strategy: the real DB (isolated Postgres
schema, see conftest.py) plus a fake Binance, checking that a strategy's
OPEN trade gets closed, flagged, or left alone depending on what the real
account balance says.

Run with: pytest -m integration
"""
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.services import reconciler
from app.models.models import BotConfig, BotStatus, OrderSide, OrderStatus, Strategy, StrategyRun, Trade, User

pytestmark = pytest.mark.integration


# ── Test double ────────────────────────────────────────────────────────────────

class FakeBinanceService:
    def __init__(self, *, balance: dict | None = None, ticker_price: float = 100.0, fills: list[dict] | None = None):
        self.balance = balance or {}
        self.ticker_price = ticker_price
        self.fills = fills or []
        self.closed = False

    async def fetch_balance(self):
        return self.balance

    async def fetch_ticker(self, symbol):
        return {"last": self.ticker_price}

    async def fetch_my_trades(self, symbol, since=None, limit=50):
        return self.fills

    async def close(self):
        self.closed = True


def _patch_binance(monkeypatch, fake: FakeBinanceService):
    monkeypatch.setattr(reconciler, "BinanceService", lambda **kwargs: fake)


# ── Helper: strategy + bot_config, real trading ──────────────────────────────────

async def _make_strategy(db_session, *, paper_trading: bool, symbol="BTC/USDT") -> tuple[Strategy, BotConfig]:
    user = User(email=f"{uuid.uuid4()}@test.local", password_hash="x")
    db_session.add(user)
    await db_session.flush()

    strategy = Strategy(
        user_id=user.id,
        name="test-strategy",
        type="sma_crossover",
        symbol=symbol,
        timeframe="1d",
        params="{}",
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
    return strategy, bot_config


async def _open_trade(db_session, strategy, *, side=OrderSide.BUY, entry_price=100.0, quantity=1.0) -> Trade:
    trade = Trade(
        strategy_id=strategy.id,
        symbol=strategy.symbol,
        side=side,
        status=OrderStatus.OPEN,
        entry_price=entry_price,
        quantity=quantity,
        opened_at=datetime.utcnow(),
    )
    db_session.add(trade)
    await db_session.commit()
    return trade


async def _runs(db_session, strategy) -> list[StrategyRun]:
    return (await db_session.execute(
        select(StrategyRun).where(StrategyRun.strategy_id == strategy.id)
    )).scalars().all()


# ── Tests ─────────────────────────────────────────────────────────────────────────

class TestPaperTradingIsSkipped:
    async def test_paper_trading_does_nothing(self, db_session, monkeypatch):
        strategy, bot_config = await _make_strategy(db_session, paper_trading=True)
        trade = await _open_trade(db_session, strategy)
        fake = FakeBinanceService(balance={"BTC": 0.0})
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["status"] == "SKIPPED"
        await db_session.refresh(trade)
        assert trade.status == OrderStatus.OPEN


class TestNoOpenTrade:
    async def test_no_trade_no_balance_is_ok(self, db_session, monkeypatch):
        strategy, bot_config = await _make_strategy(db_session, paper_trading=False)
        fake = FakeBinanceService(balance={})
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["status"] == "OK"
        assert await _runs(db_session, strategy) == []

    async def test_untracked_balance_is_flagged_not_touched(self, db_session, monkeypatch):
        strategy, bot_config = await _make_strategy(db_session, paper_trading=False)
        fake = FakeBinanceService(balance={"BTC": 0.5})
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["status"] == "MISMATCH"
        runs = await _runs(db_session, strategy)
        assert len(runs) == 1
        assert runs[0].signal == "RECONCILE_MISMATCH"


class TestOpenTradeMatchesBalance:
    async def test_matching_balance_is_left_open(self, db_session, monkeypatch):
        strategy, bot_config = await _make_strategy(db_session, paper_trading=False)
        trade = await _open_trade(db_session, strategy, quantity=1.0)
        fake = FakeBinanceService(balance={"BTC": 1.0})
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["status"] == "OK"
        await db_session.refresh(trade)
        assert trade.status == OrderStatus.OPEN
        assert await _runs(db_session, strategy) == []

    async def test_balance_within_tolerance_is_left_open(self, db_session, monkeypatch):
        # 0.5% off (fees) -- within the 1% tolerance.
        strategy, bot_config = await _make_strategy(db_session, paper_trading=False)
        trade = await _open_trade(db_session, strategy, quantity=1.0)
        fake = FakeBinanceService(balance={"BTC": 0.995})
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["status"] == "OK"
        await db_session.refresh(trade)
        assert trade.status == OrderStatus.OPEN


class TestOpenTradeClosedOutsideTheBot:
    async def test_zero_balance_closes_trade_using_real_fill_price(self, db_session, monkeypatch):
        strategy, bot_config = await _make_strategy(db_session, paper_trading=False)
        trade = await _open_trade(db_session, strategy, side=OrderSide.BUY, entry_price=100.0, quantity=1.0)
        fake = FakeBinanceService(
            balance={"BTC": 0.0},
            ticker_price=999.0,  # should be ignored -- real fills take priority
            fills=[
                {"side": "sell", "price": 108.0, "amount": 0.6},
                {"side": "sell", "price": 112.0, "amount": 0.4},
                {"side": "buy", "price": 50.0, "amount": 10.0},  # wrong side, must be ignored
            ],
        )
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["status"] == "CLOSED"
        expected_price = (108.0 * 0.6 + 112.0 * 0.4) / 1.0
        assert result["exit_price"] == pytest.approx(expected_price)

        await db_session.refresh(trade)
        assert trade.status == OrderStatus.FILLED
        assert trade.exit_price == pytest.approx(expected_price)
        assert trade.pnl == pytest.approx((expected_price - 100.0) * 1.0, abs=0.01)
        assert trade.closed_at is not None

        runs = await _runs(db_session, strategy)
        assert len(runs) == 1
        assert runs[0].signal == "RECONCILED"

    async def test_zero_balance_falls_back_to_ticker_price_without_fills(self, db_session, monkeypatch):
        strategy, bot_config = await _make_strategy(db_session, paper_trading=False)
        trade = await _open_trade(db_session, strategy, side=OrderSide.BUY, entry_price=100.0, quantity=1.0)
        fake = FakeBinanceService(balance={"BTC": 0.0}, ticker_price=105.0, fills=[])
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["status"] == "CLOSED"
        assert result["exit_price"] == pytest.approx(105.0)
        await db_session.refresh(trade)
        assert trade.status == OrderStatus.FILLED
        assert trade.exit_price == pytest.approx(105.0)

    async def test_short_trade_closing_fill_must_be_a_buy(self, db_session, monkeypatch):
        strategy, bot_config = await _make_strategy(db_session, paper_trading=False)
        trade = await _open_trade(db_session, strategy, side=OrderSide.SELL, entry_price=100.0, quantity=1.0)
        fake = FakeBinanceService(
            balance={"BTC": 0.0},
            ticker_price=999.0,
            fills=[{"side": "buy", "price": 90.0, "amount": 1.0}],
        )
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["exit_price"] == pytest.approx(90.0)
        await db_session.refresh(trade)
        # SELL trade, price dropped from 100 to 90 -> profit
        assert trade.pnl == pytest.approx(10.0)


class TestOpenTradePartialMismatch:
    async def test_significant_drift_is_flagged_and_left_open(self, db_session, monkeypatch):
        strategy, bot_config = await _make_strategy(db_session, paper_trading=False)
        trade = await _open_trade(db_session, strategy, quantity=1.0)
        # Real balance is half of what's expected -- partial close outside the bot.
        fake = FakeBinanceService(balance={"BTC": 0.5})
        _patch_binance(monkeypatch, fake)

        result = await reconciler.reconcile_strategy(strategy, bot_config, db_session)

        assert result["status"] == "MISMATCH"
        await db_session.refresh(trade)
        assert trade.status == OrderStatus.OPEN
        assert trade.exit_price is None

        runs = await _runs(db_session, strategy)
        assert len(runs) == 1
        assert runs[0].signal == "RECONCILE_MISMATCH"
