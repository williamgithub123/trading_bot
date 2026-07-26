"""
API routes for historical strategy backtesting.
"""
from datetime import date, datetime, time

import ccxt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.core.security import get_current_user
from app.models.models import User
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
):
    del current_user  # Authentication is required; market data itself is public.

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

        return run_sma_crossover_backtest(
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


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min)


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, time.max)
