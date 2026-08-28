"""Shared fixtures for the integration test suite (tests/test_scheduler_integration.py).

Those tests need a reachable Postgres — by default the same instance
docker-compose brings up for local dev (see ../docker-compose.yml), pointed at
via TEST_DATABASE_URL if you need to override it. They run against an isolated
schema ("test_integration") created at the start of the session and dropped at
the end, so they never touch the app's normal data. Nothing here is needed by
the plain unit tests (test_scheduler.py, test_strategy_engine.py, etc.) — those
have no DB dependency at all.
"""
import os

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://cryptobot:password@localhost:5432/cryptobot_db",
)
TEST_SCHEMA = "test_integration"


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Session-scoped engine bound to an isolated schema. Tables are created once
    here and the whole schema is dropped at the end — cheaper than recreating it
    per test, and per-test isolation is handled separately by db_conn's rollback."""
    from app.db.database import Base
    import app.models.models  # noqa: F401 - registers every table on Base.metadata

    admin_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with admin_engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        await conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))

    engine = admin_engine.execution_options(schema_translate_map={None: TEST_SCHEMA})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with admin_engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
    await admin_engine.dispose()


@pytest_asyncio.fixture
async def db_conn(test_engine):
    """One connection + one outer transaction per test, rolled back at teardown."""
    async with test_engine.connect() as conn:
        trans = await conn.begin()
        yield conn
        await trans.rollback()


@pytest_asyncio.fixture
def session_factory(db_conn):
    """Drop-in replacement for app.db.database.AsyncSessionLocal: every session it
    produces is bound to the same connection/outer transaction, using
    join_transaction_mode="create_savepoint" so that internal `await db.commit()`
    calls (which the app code does) release a SAVEPOINT instead of ending the
    transaction we're going to roll back at teardown."""
    return sessionmaker(
        bind=db_conn,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest_asyncio.fixture
async def db_session(session_factory):
    """A session for the test body itself (arrange fixtures, assert final state) —
    shares db_conn's transaction with whatever session run_strategy_cycle opens."""
    session = session_factory()
    yield session
    await session.close()
