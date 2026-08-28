"""
CryptoBot Backend — FastAPI Application Entry Point
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, backtest, bot, trades, strategies, market
from app.core.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is applied separately via `alembic upgrade head` (see
    # alembic/README or BACKLOG.md) before the app starts, not at import time.
    await start_scheduler()
    yield
    await stop_scheduler()


app = FastAPI(
    title="CryptoBot API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,       prefix="/api/auth",       tags=["Auth"])
app.include_router(bot.router,        prefix="/api/bot",        tags=["Bot Control"])
app.include_router(trades.router,     prefix="/api/trades",     tags=["Trades"])
app.include_router(strategies.router, prefix="/api/strategies", tags=["Strategies"])
app.include_router(market.router,     prefix="/api/market",     tags=["Market"])
app.include_router(backtest.router,   prefix="/api/backtest",   tags=["Backtesting"])


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "1.0.0"}
