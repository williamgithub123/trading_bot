"""
Database — Async SQLAlchemy session setup.

Schema is managed by Alembic (see ../../alembic/), not by this module — run
`alembic upgrade head` to create/update tables (including the TimescaleDB
hypertable + retention policy for price_candles, best-effort if the extension
isn't available). There used to be a create_all() + manual ALTER TABLE here;
see BACKLOG.md / alembic/versions/ for that history.
"""
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://cryptobot:password@localhost:5432/cryptobot_db"
)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db():
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
