"""
API Routes — Market data (price, OHLCV, balance)
Fetches live data from Binance via the user's active BotConfig
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.models.models import BotConfig, User
from app.core.security import get_current_user
from app.services.binance_service import BinanceService

router = APIRouter()


async def _get_binance(user: User, db: AsyncSession) -> BinanceService:
    result = await db.execute(
        select(BotConfig).where(BotConfig.user_id == user.id)
        .order_by(BotConfig.started_at.desc())
        .limit(1)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(404, "No bot config found. Configure the bot first.")
    return BinanceService(
        api_key_enc = config.binance_api_key_enc,
        secret_enc  = config.binance_secret_key_enc,
        testnet     = config.testnet,
    )


@router.get("/ticker/{symbol}")
async def get_ticker(
    symbol:       str,
    current_user: User         = Depends(get_current_user),
    db:           AsyncSession = Depends(get_db),
):
    binance = await _get_binance(current_user, db)
    try:
        ticker = await binance.fetch_ticker(symbol.replace("-", "/"))
        return {
            "symbol": symbol,
            "last":   ticker["last"],
            "bid":    ticker["bid"],
            "ask":    ticker["ask"],
            "change": ticker["percentage"],
            "volume": ticker["baseVolume"],
        }
    finally:
        await binance.close()


@router.get("/ohlcv/{symbol}")
async def get_ohlcv(
    symbol:       str,
    timeframe:    str = Query("1h"),
    limit:        int = Query(100, le=500),
    current_user: User         = Depends(get_current_user),
    db:           AsyncSession = Depends(get_db),
):
    binance = await _get_binance(current_user, db)
    try:
        df = await binance.fetch_ohlcv(symbol.replace("-", "/"), timeframe, limit)
        return df.to_dict(orient="records")
    finally:
        await binance.close()


@router.get("/balance")
async def get_balance(
    current_user: User         = Depends(get_current_user),
    db:           AsyncSession = Depends(get_db),
):
    binance = await _get_binance(current_user, db)
    try:
        return await binance.fetch_balance()
    finally:
        await binance.close()
