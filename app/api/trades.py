"""
API Routes — Trades
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List, Optional
import json

from app.db.database import get_db
from app.models.models import Trade, Strategy, User, OrderStatus
from app.core.security import get_current_user

router = APIRouter()


@router.get("/")
async def list_trades(
    status:       Optional[str] = Query(None),
    symbol:       Optional[str] = Query(None),
    limit:        int            = Query(50, le=200),
    current_user: User           = Depends(get_current_user),
    db:           AsyncSession   = Depends(get_db),
):
    q = (
        select(Trade)
        .join(Strategy)
        .where(Strategy.user_id == current_user.id)
        .order_by(Trade.opened_at.desc())
        .limit(limit)
    )
    if status:
        q = q.where(Trade.status == status)
    if symbol:
        q = q.where(Trade.symbol == symbol)

    result = await db.execute(q)
    trades = result.scalars().all()
    return [_trade_dict(t) for t in trades]


@router.get("/summary")
async def trade_summary(
    current_user: User         = Depends(get_current_user),
    db:           AsyncSession = Depends(get_db),
):
    q = (
        select(
            func.count(Trade.id).label("total"),
            func.sum(Trade.pnl).label("total_pnl"),
            func.avg(Trade.pnl_pct).label("avg_pnl_pct"),
        )
        .join(Strategy)
        .where(
            Strategy.user_id == current_user.id,
            Trade.status     == OrderStatus.FILLED,
            Trade.pnl        != None,
        )
    )
    row = (await db.execute(q)).one()
    return {
        "total_trades": row.total or 0,
        "total_pnl":    round(row.total_pnl or 0, 2),
        "avg_pnl_pct":  round(row.avg_pnl_pct or 0, 2),
    }


def _trade_dict(t: Trade) -> dict:
    return {
        "id":          str(t.id),
        "symbol":      t.symbol,
        "side":        t.side,
        "status":      t.status,
        "entry_price": t.entry_price,
        "exit_price":  t.exit_price,
        "quantity":    t.quantity,
        "pnl":         t.pnl,
        "pnl_pct":     t.pnl_pct,
        "stop_loss":   t.stop_loss,
        "take_profit": t.take_profit,
        "opened_at":   t.opened_at.isoformat() if t.opened_at else None,
        "closed_at":   t.closed_at.isoformat()  if t.closed_at  else None,
    }
