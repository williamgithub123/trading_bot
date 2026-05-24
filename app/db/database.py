"""
Database — Async SQLAlchemy + TimescaleDB hypertable setup
"""
import logging
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://cryptobot:password@localhost:5432/cryptobot_db"
)

logger = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def init_db():
    """Create all tables and configure TimescaleDB when available."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("""
            ALTER TABLE bot_configs
            ADD COLUMN IF NOT EXISTS paper_trading BOOLEAN NOT NULL DEFAULT TRUE;
        """))

    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb;"))
            # Convert price_candles to a TimescaleDB hypertable (idempotent)
            await conn.execute(text("""
                SELECT create_hypertable(
                    'price_candles', 'timestamp',
                    if_not_exists => TRUE
                );
            """))
            # Retention policy: keep 90 days of candle data
            await conn.execute(text("""
                SELECT add_retention_policy(
                    'price_candles',
                    INTERVAL '90 days',
                    if_not_exists => TRUE
                );
            """))
    except SQLAlchemyError as exc:
        logger.warning(
            "TimescaleDB is not available; continuing with plain PostgreSQL: %s",
            exc,
        )


async def get_db():
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
