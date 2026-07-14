"""
Backtesting service for strategy simulations.

The first implementation supports an SMA crossover long-only strategy:
- golden cross opens a long position
- death cross closes the open position
- one position at a time
- optional stop-loss support
- no fees, slippage, or take-profit yet
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
import pandas_ta as ta


@dataclass
class BacktestTrade:
    entry_at: datetime
    exit_at: datetime
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_at": self.entry_at.isoformat(),
            "exit_at": self.exit_at.isoformat(),
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "quantity": round(self.quantity, 8),
            "pnl": round(self.pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "exit_reason": self.exit_reason,
        }


@dataclass
class EquityPoint:
    timestamp: datetime
    equity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "equity": round(self.equity, 4),
        }


def run_sma_crossover_backtest(
    candles: pd.DataFrame,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1d",
    fast: int = 7,
    slow: int = 25,
    initial_capital: float = 1000.0,
    stop_loss_pct: float | None = None,
) -> dict[str, Any]:
    """Run a simple SMA crossover backtest over historical OHLCV candles."""
    _validate_inputs(candles, fast, slow, initial_capital, stop_loss_pct)

    df = candles.copy().sort_values("timestamp").reset_index(drop=True)
    candles_count = len(df)
    candle_start = _to_datetime(df.iloc[0]["timestamp"]) if candles_count else None
    candle_end = _to_datetime(df.iloc[-1]["timestamp"]) if candles_count else None
    df["sma_fast"] = ta.sma(df["close"], length=fast)
    df["sma_slow"] = ta.sma(df["close"], length=slow)
    df = df.dropna(subset=["sma_fast", "sma_slow"]).reset_index(drop=True)

    if len(df) < 2:
        return _empty_result(
            symbol,
            timeframe,
            fast,
            slow,
            initial_capital,
            stop_loss_pct,
            candles_count,
            candle_start,
            candle_end,
        )

    cash = initial_capital
    quantity = 0.0
    entry_price = 0.0
    entry_at: datetime | None = None
    trades: list[BacktestTrade] = []
    equity_curve: list[EquityPoint] = []

    for index in range(1, len(df)):
        prev = df.iloc[index - 1]
        curr = df.iloc[index]
        price = float(curr["close"])
        timestamp = _to_datetime(curr["timestamp"])

        golden_cross = (
            prev["sma_fast"] <= prev["sma_slow"]
            and curr["sma_fast"] > curr["sma_slow"]
        )
        death_cross = (
            prev["sma_fast"] >= prev["sma_slow"]
            and curr["sma_fast"] < curr["sma_slow"]
        )

        exit_reason = None
        exit_price = price

        if quantity > 0 and entry_at is not None and stop_loss_pct is not None:
            stop_price = entry_price * (1 - (stop_loss_pct / 100))
            if _stop_loss_hit(curr, stop_price):
                exit_reason = "stop_loss"
                exit_price = stop_price

        if exit_reason is None and quantity > 0 and death_cross:
            exit_reason = "death_cross"

        if quantity > 0 and exit_reason is not None and entry_at is not None:
            cash = quantity * exit_price
            trade_cost = quantity * entry_price
            pnl = cash - trade_cost
            pnl_pct = (pnl / trade_cost) * 100 if trade_cost else 0.0
            trades.append(
                BacktestTrade(
                    entry_at=entry_at,
                    exit_at=timestamp,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    quantity=quantity,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    exit_reason=exit_reason,
                )
            )
            quantity = 0.0
            entry_price = 0.0
            entry_at = None
        elif quantity == 0 and golden_cross:
            entry_price = price
            entry_at = timestamp
            quantity = cash / price
            cash = 0.0

        equity = cash if quantity == 0 else quantity * price
        equity_curve.append(EquityPoint(timestamp=timestamp, equity=equity))

    final_price = float(df.iloc[-1]["close"])
    final_equity = cash if quantity == 0 else quantity * final_price
    return _build_result(
        symbol=symbol,
        timeframe=timeframe,
        fast=fast,
        slow=slow,
        initial_capital=initial_capital,
        stop_loss_pct=stop_loss_pct,
        candles_count=candles_count,
        candle_start=candle_start,
        candle_end=candle_end,
        final_equity=final_equity,
        trades=trades,
        equity_curve=equity_curve,
    )


def _validate_inputs(
    candles: pd.DataFrame,
    fast: int,
    slow: int,
    initial_capital: float,
    stop_loss_pct: float | None,
) -> None:
    required = {"timestamp", "close"}
    missing = required - set(candles.columns)
    if missing:
        raise ValueError(f"Missing required candle columns: {sorted(missing)}")
    if fast <= 0 or slow <= 0:
        raise ValueError("SMA periods must be positive")
    if fast >= slow:
        raise ValueError("Fast SMA period must be lower than slow SMA period")
    if initial_capital <= 0:
        raise ValueError("Initial capital must be positive")
    if stop_loss_pct is not None and stop_loss_pct <= 0:
        raise ValueError("Stop-loss percentage must be positive")


def _build_result(
    *,
    symbol: str,
    timeframe: str,
    fast: int,
    slow: int,
    initial_capital: float,
    stop_loss_pct: float | None,
    candles_count: int,
    candle_start: datetime | None,
    candle_end: datetime | None,
    final_equity: float,
    trades: list[BacktestTrade],
    equity_curve: list[EquityPoint],
) -> dict[str, Any]:
    total_trades = len(trades)
    winning_trades = len([trade for trade in trades if trade.pnl > 0])
    losing_trades = len([trade for trade in trades if trade.pnl < 0])
    pnl = final_equity - initial_capital
    pnl_pct = (pnl / initial_capital) * 100
    win_rate = (winning_trades / total_trades) * 100 if total_trades else 0.0

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "fast": fast,
        "slow": slow,
        "initial_capital": round(initial_capital, 4),
        "stop_loss_pct": round(stop_loss_pct, 4) if stop_loss_pct is not None else None,
        "candles_count": candles_count,
        "candle_start": candle_start.isoformat() if candle_start else None,
        "candle_end": candle_end.isoformat() if candle_end else None,
        "final_equity": round(final_equity, 4),
        "pnl": round(pnl, 4),
        "pnl_pct": round(pnl_pct, 4),
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": round(win_rate, 4),
        "max_drawdown_pct": round(_max_drawdown_pct(equity_curve), 4),
        "trades": [trade.to_dict() for trade in trades],
        "equity_curve": [point.to_dict() for point in equity_curve],
    }


def _empty_result(
    symbol: str,
    timeframe: str,
    fast: int,
    slow: int,
    initial_capital: float,
    stop_loss_pct: float | None,
    candles_count: int = 0,
    candle_start: datetime | None = None,
    candle_end: datetime | None = None,
) -> dict[str, Any]:
    return _build_result(
        symbol=symbol,
        timeframe=timeframe,
        fast=fast,
        slow=slow,
        initial_capital=initial_capital,
        stop_loss_pct=stop_loss_pct,
        candles_count=candles_count,
        candle_start=candle_start,
        candle_end=candle_end,
        final_equity=initial_capital,
        trades=[],
        equity_curve=[],
    )


def _max_drawdown_pct(equity_curve: list[EquityPoint]) -> float:
    peak = 0.0
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point.equity)
        if peak == 0:
            continue
        drawdown = ((peak - point.equity) / peak) * 100
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return pd.to_datetime(value).to_pydatetime()


def _stop_loss_hit(row: pd.Series, stop_price: float) -> bool:
    if "low" in row and pd.notna(row["low"]):
        return float(row["low"]) <= stop_price
    return float(row["close"]) <= stop_price
