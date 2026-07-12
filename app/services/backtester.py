"""
Backtesting service for strategy simulations.

The first implementation supports an SMA crossover long-only strategy:
- golden cross opens a long position
- death cross closes the open position
- one position at a time
- no fees, slippage, stop-loss, or take-profit yet
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_at": self.entry_at.isoformat(),
            "exit_at": self.exit_at.isoformat(),
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "quantity": round(self.quantity, 8),
            "pnl": round(self.pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
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
) -> dict[str, Any]:
    """Run a simple SMA crossover backtest over historical OHLCV candles."""
    _validate_inputs(candles, fast, slow, initial_capital)

    df = candles.copy().sort_values("timestamp").reset_index(drop=True)
    df["sma_fast"] = ta.sma(df["close"], length=fast)
    df["sma_slow"] = ta.sma(df["close"], length=slow)
    df = df.dropna(subset=["sma_fast", "sma_slow"]).reset_index(drop=True)

    if len(df) < 2:
        return _empty_result(symbol, timeframe, fast, slow, initial_capital)

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

        if quantity == 0 and golden_cross:
            entry_price = price
            entry_at = timestamp
            quantity = cash / price
            cash = 0.0
        elif quantity > 0 and death_cross and entry_at is not None:
            cash = quantity * price
            trade_cost = quantity * entry_price
            pnl = cash - trade_cost
            pnl_pct = (pnl / trade_cost) * 100 if trade_cost else 0.0
            trades.append(
                BacktestTrade(
                    entry_at=entry_at,
                    exit_at=timestamp,
                    entry_price=entry_price,
                    exit_price=price,
                    quantity=quantity,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                )
            )
            quantity = 0.0
            entry_price = 0.0
            entry_at = None

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
        final_equity=final_equity,
        trades=trades,
        equity_curve=equity_curve,
    )


def _validate_inputs(
    candles: pd.DataFrame,
    fast: int,
    slow: int,
    initial_capital: float,
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


def _build_result(
    *,
    symbol: str,
    timeframe: str,
    fast: int,
    slow: int,
    initial_capital: float,
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
) -> dict[str, Any]:
    return _build_result(
        symbol=symbol,
        timeframe=timeframe,
        fast=fast,
        slow=slow,
        initial_capital=initial_capital,
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
