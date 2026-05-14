"""
API Routes — Strategies
"""
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional

from app.db.database import get_db
from app.models.models import Strategy, User
from app.core.security import get_current_user

router = APIRouter()


class StrategyRequest(BaseModel):
    name:              str
    type:              str        # sma_crossover | rsi | macd
    symbol:            str        # BTC/USDT
    timeframe:         str        # 1h
    params:            dict       # strategy-specific params
    max_position_pct:  float = 10.0
    stop_loss_pct:     float = 2.0
    take_profit_pct:   float = 4.0


@router.get("/")
async def list_strategies(
    current_user: User         = Depends(get_current_user),
    db:           AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.user_id == current_user.id)
    )
    return [_strat_dict(s) for s in result.scalars().all()]


@router.post("/", status_code=201)
async def create_strategy(
    body:         StrategyRequest,
    current_user: User             = Depends(get_current_user),
    db:           AsyncSession     = Depends(get_db),
):
    strategy = Strategy(
        user_id         = current_user.id,
        name            = body.name,
        type            = body.type,
        symbol          = body.symbol,
        timeframe       = body.timeframe,
        params          = json.dumps(body.params),
        max_position_pct = body.max_position_pct,
        stop_loss_pct   = body.stop_loss_pct,
        take_profit_pct = body.take_profit_pct,
    )
    db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return _strat_dict(strategy)


@router.put("/{strategy_id}")
async def update_strategy(
    strategy_id:  str,
    body:         StrategyRequest,
    current_user: User             = Depends(get_current_user),
    db:           AsyncSession     = Depends(get_db),
):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy or str(strategy.user_id) != str(current_user.id):
        raise HTTPException(404, "Strategy not found")

    strategy.name             = body.name
    strategy.type             = body.type
    strategy.symbol           = body.symbol
    strategy.timeframe        = body.timeframe
    strategy.params           = json.dumps(body.params)
    strategy.max_position_pct = body.max_position_pct
    strategy.stop_loss_pct    = body.stop_loss_pct
    strategy.take_profit_pct  = body.take_profit_pct
    await db.commit()
    await db.refresh(strategy)
    return _strat_dict(strategy)


@router.delete("/{strategy_id}", status_code=204)
async def delete_strategy(
    strategy_id:  str,
    current_user: User         = Depends(get_current_user),
    db:           AsyncSession = Depends(get_db),
):
    strategy = await db.get(Strategy, strategy_id)
    if not strategy or str(strategy.user_id) != str(current_user.id):
        raise HTTPException(404, "Strategy not found")
    await db.delete(strategy)
    await db.commit()


def _strat_dict(s: Strategy) -> dict:
    return {
        "id":               str(s.id),
        "name":             s.name,
        "type":             s.type,
        "symbol":           s.symbol,
        "timeframe":        s.timeframe,
        "params":           json.loads(s.params),
        "max_position_pct": s.max_position_pct,
        "stop_loss_pct":    s.stop_loss_pct,
        "take_profit_pct":  s.take_profit_pct,
        "is_active":        s.is_active,
    }
