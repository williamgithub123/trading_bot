"""
Database Models — SQLAlchemy ORM
Compatible with PostgreSQL / TimescaleDB
"""
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Boolean, DateTime,
    ForeignKey, Enum, Text, Integer
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

    exchange_order_id = Column(String(100), nullable=True)
    symbol            = Column(String(20), nullable=False)
    side              = Column(Enum(OrderSide), nullable=False)
    status            = Column(Enum(OrderStatus), default=OrderStatus.OPEN)

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


# ─── PriceCandle (TimescaleDB hypertable) ────────────────────────────────────

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
