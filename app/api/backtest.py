"""
API routes for historical strategy backtesting.
"""
import json
from datetime import date, datetime, time
from uuid import UUID as UUIDValue

import ccxt
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.db.database import get_db
from app.models.models import BacktestRun, User
from app.services.backtester import run_sma_crossover_backtest
from app.services.binance_service import BinanceService

router = APIRouter()


class SmaBacktestRequest(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "1d"
    fast: int = Field(7, ge=2, le=500)
    slow: int = Field(25, ge=3, le=500)
    limit: int = Field(365, ge=50, le=1000)
    start_date: date | None = None
    end_date: date | None = None
    max_candles: int = Field(20000, ge=100, le=50000)
    initial_capital: float = Field(1000.0, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, le=100)
    trend_filter_enabled: bool = False
    trend_sma_period: int = Field(200, ge=4, le=1000)
    trend_require_rising: bool = False

    @model_validator(mode="after")
    def validate_sma_periods(self):
        if self.fast >= self.slow:
            raise ValueError("Fast SMA period must be lower than slow SMA period")
        if self.trend_filter_enabled and self.trend_sma_period <= self.slow:
            raise ValueError("Trend SMA period must be greater than slow SMA period")
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be provided together")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date")
        required_limit = self.trend_sma_period if self.trend_filter_enabled else self.slow
        if self.start_date is None and self.limit <= required_limit:
            raise ValueError("Candle limit must be greater than the largest SMA period")
        return self


@router.post("/sma-crossover")
async def run_sma_backtest(
    body: SmaBacktestRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    symbol = body.symbol.strip().upper().replace("-", "/")
    binance = BinanceService(
        api_key_enc="",
        secret_enc="",
        testnet=False,
        use_credentials=False,
    )

    try:
        if body.start_date and body.end_date:
            until = _end_of_day(body.end_date)
            candles = await binance.fetch_ohlcv_range(
                symbol=symbol,
                timeframe=body.timeframe,
                since=_start_of_day(body.start_date),
                until=until,
                max_candles=body.max_candles,
            )
            if (
                not candles.empty
                and len(candles) >= body.max_candles
                and candles.iloc[-1]["timestamp"] < until
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "The requested date range was truncated by max_candles. "
                        "Increase max_candles or use a shorter date range."
                    ),
                )
        else:
            candles = await binance.fetch_ohlcv(
                symbol=symbol,
                timeframe=body.timeframe,
                limit=body.limit,
            )

        result = run_sma_crossover_backtest(
            candles,
            symbol=symbol,
            timeframe=body.timeframe,
            fast=body.fast,
            slow=body.slow,
            initial_capital=body.initial_capital,
            stop_loss_pct=body.stop_loss_pct,
            trend_filter_enabled=body.trend_filter_enabled,
            trend_sma_period=body.trend_sma_period,
            trend_require_rising=body.trend_require_rising,
        )
        backtest_run = await _save_backtest_run(
            db=db,
            user=current_user,
            body=body,
            symbol=symbol,
            result=result,
        )
        result["backtest_run_id"] = str(backtest_run.id)
        return result
    except ccxt.BadSymbol as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Binance does not recognize symbol {symbol}",
        ) from exc
    except (ccxt.BadRequest, ccxt.ArgumentsRequired) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ccxt.NetworkError as exc:
        raise HTTPException(
            status_code=503,
            detail="Binance market data is temporarily unavailable",
        ) from exc
    finally:
        await binance.close()


@router.get("/runs")
async def list_backtest_runs(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(BacktestRun)
        .where(BacktestRun.user_id == current_user.id)
        .order_by(BacktestRun.created_at.desc())
        .limit(limit)
    )
    if symbol:
        query = query.where(BacktestRun.symbol == symbol.strip().upper().replace("-", "/"))
    if timeframe:
        query = query.where(BacktestRun.timeframe == timeframe.strip())

    result = await db.execute(query)
    return [_backtest_summary_dict(run) for run in result.scalars().all()]


@router.get("/runs/{run_id}")
async def get_backtest_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    parsed_run_id = _parse_uuid(run_id)
    result = await db.execute(
        select(BacktestRun).where(
            BacktestRun.id == parsed_run_id,
            BacktestRun.user_id == current_user.id,
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Backtest run not found")
    return _backtest_detail_dict(run)


@router.delete("/runs/{run_id}", status_code=204)
async def delete_backtest_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    parsed_run_id = _parse_uuid(run_id)
    result = await db.execute(
        select(BacktestRun).where(
            BacktestRun.id == parsed_run_id,
            BacktestRun.user_id == current_user.id,
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(404, "Backtest run not found")
    await db.delete(run)
    await db.commit()


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min)


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, time.max)


async def _save_backtest_run(
    *,
    db: AsyncSession,
    user: User,
    body: SmaBacktestRequest,
    symbol: str,
    result: dict,
) -> BacktestRun:
    comparison = result.get("comparison") or {}
    buy_and_hold = result.get("buy_and_hold") or {}

    run = BacktestRun(
        user_id=user.id,
        strategy_type="sma_crossover",
        symbol=symbol,
        timeframe=body.timeframe,
        fast=body.fast,
        slow=body.slow,
        start_date=body.start_date,
        end_date=body.end_date,
        limit=body.limit if body.start_date is None else None,
        max_candles=body.max_candles if body.start_date else None,
        initial_capital=body.initial_capital,
        stop_loss_pct=body.stop_loss_pct,
        trend_filter_enabled=body.trend_filter_enabled,
        trend_sma_period=body.trend_sma_period if body.trend_filter_enabled else None,
        trend_require_rising=body.trend_require_rising if body.trend_filter_enabled else False,
        candles_count=result.get("candles_count", 0),
        candle_start=_parse_optional_datetime(result.get("candle_start")),
        candle_end=_parse_optional_datetime(result.get("candle_end")),
        final_equity=result.get("final_equity", body.initial_capital),
        pnl=result.get("pnl", 0.0),
        pnl_pct=result.get("pnl_pct", 0.0),
        total_trades=result.get("total_trades", 0),
        winning_trades=result.get("winning_trades", 0),
        losing_trades=result.get("losing_trades", 0),
        win_rate=result.get("win_rate", 0.0),
        max_drawdown_pct=result.get("max_drawdown_pct", 0.0),
        buy_and_hold_pnl_pct=buy_and_hold.get("pnl_pct", 0.0),
        alpha_pct=comparison.get("alpha_pct", 0.0),
        request_json=json.dumps(jsonable_encoder(body.model_dump())),
        result_json=json.dumps(jsonable_encoder(result)),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _parse_uuid(value: str) -> UUIDValue:
    try:
        return UUIDValue(value)
    except ValueError as exc:
        raise HTTPException(400, "Invalid backtest run id") from exc


def _backtest_summary_dict(run: BacktestRun) -> dict:
    return {
        "id": str(run.id),
        "strategy_type": run.strategy_type,
        "symbol": run.symbol,
        "timeframe": run.timeframe,
        "fast": run.fast,
        "slow": run.slow,
        "start_date": run.start_date.isoformat() if run.start_date else None,
        "end_date": run.end_date.isoformat() if run.end_date else None,
        "initial_capital": run.initial_capital,
        "stop_loss_pct": run.stop_loss_pct,
        "trend_filter_enabled": run.trend_filter_enabled,
        "trend_sma_period": run.trend_sma_period,
        "trend_require_rising": run.trend_require_rising,
        "candles_count": run.candles_count,
        "final_equity": run.final_equity,
        "pnl": run.pnl,
        "pnl_pct": run.pnl_pct,
        "total_trades": run.total_trades,
        "win_rate": run.win_rate,
        "max_drawdown_pct": run.max_drawdown_pct,
        "buy_and_hold_pnl_pct": run.buy_and_hold_pnl_pct,
        "alpha_pct": run.alpha_pct,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _backtest_detail_dict(run: BacktestRun) -> dict:
    return {
        **_backtest_summary_dict(run),
        "request": json.loads(run.request_json or "{}"),
        "result": json.loads(run.result_json or "{}"),
    }
