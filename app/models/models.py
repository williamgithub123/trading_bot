"""
Database Models — SQLAlchemy ORM
Compatible with PostgreSQL / TimescaleDB
"""
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Boolean, DateTime,
    ForeignKey, Enum, Text, Integer, Date
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import uuid
import enum

from app.db.database import Base


class OrderSide(str, enum.Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderStatus(str, enum.Enum):
    OPEN     = "OPEN"
    FILLED   = "FILLED"
    CANCELED = "CANCELED"
    FAILED   = "FAILED"


class BotStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    PAUSED  = "PAUSED"
    STOPPED = "STOPPED"


# ─── User ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email         = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)

    strategies    = relationship("Strategy", back_populates="user", cascade="all, delete")
    bot_configs   = relationship("BotConfig", back_populates="user", cascade="all, delete")
    backtest_runs = relationship("BacktestRun", back_populates="user", cascade="all, delete")


# ─── Strategy ────────────────────────────────────────────────────────────────

class Strategy(Base):
    __tablename__ = "strategies"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id        = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name           = Column(String(100), nullable=False)
    type           = Column(String(50), nullable=False)   # sma_crossover, rsi, macd
    symbol         = Column(String(20), nullable=False)   # BTC/USDT
    timeframe      = Column(String(10), nullable=False)   # 1m, 5m, 1h, 1d

    # JSON-serialised strategy-specific parameters
    params         = Column(Text, nullable=False, default="{}")

    # Risk management
    max_position_pct = Column(Float, default=10.0)   # % of portfolio per trade
    stop_loss_pct    = Column(Float, default=2.0)    # % below entry
    take_profit_pct  = Column(Float, default=4.0)    # % above entry

    is_active      = Column(Boolean, default=False)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user       = relationship("User", back_populates="strategies")
    bot_config = relationship("BotConfig", back_populates="strategy", uselist=False)
    trades     = relationship("Trade", back_populates="strategy")
    runs       = relationship("StrategyRun", back_populates="strategy", cascade="all, delete")


# ─── BotConfig ───────────────────────────────────────────────────────────────

class BotConfig(Base):
    __tablename__ = "bot_configs"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id     = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    strategy_id = Column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False)

    # Binance credentials (stored encrypted — see core/security.py)
    binance_api_key_enc    = Column(Text, nullable=False)
    binance_secret_key_enc = Column(Text, nullable=False)
    testnet                = Column(Boolean, default=True)
    paper_trading          = Column(Boolean, default=True)

    status     = Column(Enum(BotStatus), default=BotStatus.STOPPED)
    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)

    user     = relationship("User", back_populates="bot_configs")
    strategy = relationship("Strategy", back_populates="bot_config")


# ─── Trade ───────────────────────────────────────────────────────────────────

class Trade(Base):
    __tablename__ = "trades"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id = Column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False)

    exchange_order_id   = Column(String(100), nullable=True)
    # Real STOP_LOSS_LIMIT order placed on Binance for this trade (paper_trading=False
    # only) -- see app/services/order_executor.py / app/services/reconciler.py.
    stop_loss_order_id  = Column(String(100), nullable=True)
    symbol              = Column(String(20), nullable=False)
    side                = Column(Enum(OrderSide), nullable=False)
    status              = Column(Enum(OrderStatus), default=OrderStatus.OPEN)

    entry_price  = Column(Float, nullable=False)
    exit_price   = Column(Float, nullable=True)
    quantity     = Column(Float, nullable=False)
    pnl          = Column(Float, nullable=True)     # Realised P&L in USDT
    pnl_pct      = Column(Float, nullable=True)     # P&L as %

    stop_loss    = Column(Float, nullable=True)
    take_profit  = Column(Float, nullable=True)

    opened_at    = Column(DateTime, default=datetime.utcnow, index=True)
    closed_at    = Column(DateTime, nullable=True)

    strategy     = relationship("Strategy", back_populates="trades")


# ─── StrategyRun ─────────────────────────────────────────────────────────────

class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id = Column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True)

    signal     = Column(String(20), nullable=False)
    reason     = Column(Text, nullable=False)
    indicators = Column(Text, nullable=False, default="{}")
    price      = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    strategy = relationship("Strategy", back_populates="runs")


# ─── PriceCandle (TimescaleDB hypertable) ────────────────────────────────────

class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id      = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)

    strategy_type = Column(String(50), nullable=False, default="sma_crossover")
    symbol        = Column(String(20), nullable=False, index=True)
    timeframe     = Column(String(10), nullable=False, index=True)

    fast            = Column(Integer, nullable=False)
    slow            = Column(Integer, nullable=False)
    start_date      = Column(Date, nullable=True)
    end_date        = Column(Date, nullable=True)
    limit           = Column(Integer, nullable=True)
    max_candles     = Column(Integer, nullable=True)
    initial_capital = Column(Float, nullable=False)

    stop_loss_pct        = Column(Float, nullable=True)
    trend_filter_enabled = Column(Boolean, default=False)
    trend_sma_period     = Column(Integer, nullable=True)
    trend_require_rising = Column(Boolean, default=False)

    candles_count        = Column(Integer, default=0)
    candle_start         = Column(DateTime, nullable=True)
    candle_end           = Column(DateTime, nullable=True)
    final_equity         = Column(Float, nullable=False)
    pnl                  = Column(Float, nullable=False)
    pnl_pct              = Column(Float, nullable=False)
    total_trades         = Column(Integer, default=0)
    winning_trades       = Column(Integer, default=0)
    losing_trades        = Column(Integer, default=0)
    win_rate             = Column(Float, default=0.0)
    max_drawdown_pct     = Column(Float, default=0.0)
    buy_and_hold_pnl_pct = Column(Float, default=0.0)
    alpha_pct            = Column(Float, default=0.0)

    request_json = Column(Text, nullable=False, default="{}")
    result_json  = Column(Text, nullable=False, default="{}")
    created_at   = Column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", back_populates="backtest_runs")


class PriceCandle(Base):
    __tablename__ = "price_candles"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    symbol    = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)

    open      = Column(Float, nullable=False)
    high      = Column(Float, nullable=False)
    low       = Column(Float, nullable=False)
    close     = Column(Float, nullable=False)
    volume    = Column(Float, nullable=False)
