"""
API routes for historical strategy backtesting.
"""
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
    initial_capital: float = Field(1000.0, gt=0)
    stop_loss_pct: float | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def validate_sma_periods(self):
        if self.fast >= self.slow:
            raise ValueError("Fast SMA period must be lower than slow SMA period")
        if self.limit <= self.slow:
            raise ValueError("Candle limit must be greater than slow SMA period")
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
