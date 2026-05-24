"""
API Routes — Bot Control (start / pause / stop / status)
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime

from app.db.database import get_db
from app.models.models import BotConfig, BotStatus, Strategy, User
from app.core.security import get_current_user, encrypt_key
from app.core.scheduler import run_strategy_cycle, schedule_strategy, unschedule_strategy

router = APIRouter()


class BotConfigRequest(BaseModel):
    strategy_id:     str
    binance_api_key: str
    binance_secret:  str
    testnet:         bool = True
    paper_trading:   bool = True


class BotActionRequest(BaseModel):
    config_id: str


@router.post("/configure")
async def configure_bot(
    body:         BotConfigRequest,
    current_user: User             = Depends(get_current_user),
    db:           AsyncSession     = Depends(get_db),
):
    strategy = await db.get(Strategy, body.strategy_id)
    if not strategy or str(strategy.user_id) != str(current_user.id):
        raise HTTPException(404, "Strategy not found")

    config = BotConfig(
        user_id                = current_user.id,
        strategy_id            = body.strategy_id,
        binance_api_key_enc    = encrypt_key(body.binance_api_key),
        binance_secret_key_enc = encrypt_key(body.binance_secret),
        testnet                = body.testnet,
        paper_trading          = body.paper_trading,
        status                 = BotStatus.STOPPED,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return {
        "config_id": str(config.id),
        "status": config.status,
        "testnet": config.testnet,
        "paper_trading": config.paper_trading,
    }


@router.post("/start")
async def start_bot(
    body:         BotActionRequest,
    current_user: User             = Depends(get_current_user),
    db:           AsyncSession     = Depends(get_db),
):
    config = await db.get(BotConfig, body.config_id)
    if not config or str(config.user_id) != str(current_user.id):
        raise HTTPException(404, "Config not found")

    strategy = await db.get(Strategy, config.strategy_id)

    config.status     = BotStatus.RUNNING
    config.started_at = datetime.utcnow()
    strategy.is_active = True
    await db.commit()

    await schedule_strategy(str(strategy.id), strategy.timeframe)
    return {"status": "RUNNING"}


@router.post("/pause")
async def pause_bot(
    body:         BotActionRequest,
    current_user: User             = Depends(get_current_user),
    db:           AsyncSession     = Depends(get_db),
):
    config = await db.get(BotConfig, body.config_id)
    if not config or str(config.user_id) != str(current_user.id):
        raise HTTPException(404, "Config not found")

    config.status = BotStatus.PAUSED
    await db.commit()
    await unschedule_strategy(str(config.strategy_id))
    return {"status": "PAUSED"}


@router.post("/stop")
async def stop_bot(
    body:         BotActionRequest,
    current_user: User             = Depends(get_current_user),
    db:           AsyncSession     = Depends(get_db),
):
    config = await db.get(BotConfig, body.config_id)
    if not config or str(config.user_id) != str(current_user.id):
        raise HTTPException(404, "Config not found")

    strategy = await db.get(Strategy, config.strategy_id)
    config.status      = BotStatus.STOPPED
    config.stopped_at  = datetime.utcnow()
    strategy.is_active = False
    await db.commit()
    await unschedule_strategy(str(config.strategy_id))
    return {"status": "STOPPED"}


@router.post("/run-once")
async def run_once(
    body:         BotActionRequest,
    current_user: User             = Depends(get_current_user),
    db:           AsyncSession     = Depends(get_db),
):
    config = await db.get(BotConfig, body.config_id)
    if not config or str(config.user_id) != str(current_user.id):
        raise HTTPException(404, "Config not found")
    if not config.paper_trading:
        raise HTTPException(400, "Run-once is only available in paper trading mode")

    result = await run_strategy_cycle(str(config.strategy_id), force=True)
    return result


@router.get("/status/{config_id}")
async def get_status(
    config_id:    str,
    current_user: User         = Depends(get_current_user),
    db:           AsyncSession = Depends(get_db),
):
    config = await db.get(BotConfig, config_id)
    if not config or str(config.user_id) != str(current_user.id):
        raise HTTPException(404, "Config not found")
    return {
        "status":     config.status,
        "started_at": config.started_at,
        "stopped_at": config.stopped_at,
        "testnet":    config.testnet,
        "paper_trading": config.paper_trading,
    }
