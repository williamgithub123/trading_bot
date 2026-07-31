"""
Strategy Engine — SMA Crossover, RSI, MACD signal generators
Each strategy returns a Signal: BUY | SELL | HOLD
"""
import json
from enum import Enum
from dataclasses import dataclass
from typing import Optional
import pandas as pd
import pandas_ta as ta


class Signal(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class StrategyResult:
    signal:     Signal
    confidence: float       # 0.0 – 1.0
    reason:     str
    indicators: dict        # raw indicator values for logging


# ── SMA Crossover ─────────────────────────────────────────────────────────────

def sma_crossover(df: pd.DataFrame, params: dict) -> StrategyResult:
    """
    Classic dual moving-average crossover.
    params: {
      "fast": 20,
      "slow": 50,
      "trend_filter_enabled": true,
      "trend_sma_period": 100,
      "trend_require_rising": false
    }
    """
    fast = params.get("fast", 20)
    slow = params.get("slow", 50)
    trend_filter_enabled = params.get("trend_filter_enabled", True)
    trend_sma_period = params.get("trend_sma_period", 100)
    trend_require_rising = params.get("trend_require_rising", False)

    df = df.copy()
    df["sma_fast"] = ta.sma(df["close"], length=fast)
    df["sma_slow"] = ta.sma(df["close"], length=slow)
    required_columns = ["sma_fast", "sma_slow"]
    if trend_filter_enabled:
        df["sma_trend"] = ta.sma(df["close"], length=trend_sma_period)
        required_columns.append("sma_trend")
    df = df.dropna(subset=required_columns)

    if len(df) < 2:
        return StrategyResult(
            Signal.HOLD,
            0.0,
            f"Not enough data for SMA{fast}/SMA{slow}",
            {},
        )

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    golden_cross = prev["sma_fast"] <= prev["sma_slow"] and curr["sma_fast"] > curr["sma_slow"]
    death_cross  = prev["sma_fast"] >= prev["sma_slow"] and curr["sma_fast"] < curr["sma_slow"]
    sma_spread = curr["sma_fast"] - curr["sma_slow"]
    sma_spread_pct = (sma_spread / curr["sma_slow"]) * 100 if curr["sma_slow"] else 0
    if sma_spread > 0:
        trend_bias = "bullish"
        trend_label = "Fast SMA above slow SMA"
    elif sma_spread < 0:
        trend_bias = "bearish"
        trend_label = "Fast SMA below slow SMA"
    else:
        trend_bias = "neutral"
        trend_label = "Fast and slow SMA aligned"

    indicators = {
        "sma_fast": float(round(curr["sma_fast"], 4)),
        "sma_slow": float(round(curr["sma_slow"], 4)),
        "price":    float(round(curr["close"], 4)),
        "trend_bias": trend_bias,
        "trend_label": trend_label,
        "sma_spread": float(round(sma_spread, 4)),
        "sma_spread_pct": float(round(sma_spread_pct, 4)),
    }
    if trend_filter_enabled:
        trend_allows_entry = _trend_allows_entry(
            prev,
            curr,
            require_rising=trend_require_rising,
        )
        indicators.update({
            "trend_filter_enabled": True,
            "sma_trend": float(round(curr["sma_trend"], 4)),
            "trend_sma_period": trend_sma_period,
            "trend_require_rising": trend_require_rising,
            "price_above_trend": bool(float(curr["close"]) > float(curr["sma_trend"])),
            "trend_allows_entry": trend_allows_entry,
        })
    else:
        trend_allows_entry = True
        indicators["trend_filter_enabled"] = False

    if golden_cross:
        if not trend_allows_entry:
            return StrategyResult(
                Signal.HOLD,
                0.0,
                f"Golden cross SMA{fast}/SMA{slow} skipped by trend filter",
                indicators,
            )
        return StrategyResult(Signal.BUY,  0.75, f"Golden cross SMA{fast}/SMA{slow} - {trend_bias} bias", indicators)
    if death_cross:
        return StrategyResult(Signal.SELL, 0.75, f"Death cross SMA{fast}/SMA{slow} - {trend_bias} bias",  indicators)
    return StrategyResult(Signal.HOLD, 0.0, f"No crossover - {trend_bias} bias", indicators)


def _trend_allows_entry(prev: pd.Series, curr: pd.Series, *, require_rising: bool) -> bool:
    price_above_trend = float(curr["close"]) > float(curr["sma_trend"])
    if not price_above_trend:
        return False
    if require_rising:
        return float(curr["sma_trend"]) > float(prev["sma_trend"])
    return True


# ── RSI Strategy ──────────────────────────────────────────────────────────────

def rsi_strategy(df: pd.DataFrame, params: dict) -> StrategyResult:
    """
    Oversold/overbought RSI.
    params: { "period": 14, "oversold": 30, "overbought": 70 }
    """
    period     = params.get("period",     14)
    oversold   = params.get("oversold",   30)
    overbought = params.get("overbought", 70)

    df = df.copy()
    df["rsi"] = ta.rsi(df["close"], length=period)
    df = df.dropna(subset=["rsi"])

    if len(df) < 2:
        return StrategyResult(
            Signal.HOLD,
            0.0,
            f"Not enough data for RSI{period}",
            {},
        )

    curr_rsi = df["rsi"].iloc[-1]
    prev_rsi = df["rsi"].iloc[-2]

    indicators = {
        "rsi": float(round(curr_rsi, 2)),
        "price": float(round(df["close"].iloc[-1], 4)),
    }

    if prev_rsi < oversold and curr_rsi >= oversold:
        return StrategyResult(Signal.BUY,  0.8, f"RSI recovering from oversold ({curr_rsi:.1f})", indicators)
    if prev_rsi > overbought and curr_rsi <= overbought:
        return StrategyResult(Signal.SELL, 0.8, f"RSI leaving overbought ({curr_rsi:.1f})", indicators)
    return StrategyResult(Signal.HOLD, 0.0, f"RSI neutral ({curr_rsi:.1f})", indicators)


# ── MACD Strategy ─────────────────────────────────────────────────────────────

def macd_strategy(df: pd.DataFrame, params: dict) -> StrategyResult:
    """
    MACD histogram crossover.
    params: { "fast": 12, "slow": 26, "signal": 9 }
    """
    fast   = params.get("fast",   12)
    slow   = params.get("slow",   26)
    signal = params.get("signal",  9)

    df = df.copy()
    macd_df = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
    df = pd.concat([df, macd_df], axis=1)

    hist_col = f"MACDh_{fast}_{slow}_{signal}"
    df = df.dropna(subset=[hist_col])

    if len(df) < 2:
        return StrategyResult(
            Signal.HOLD,
            0.0,
            f"Not enough data for MACD {fast}/{slow}/{signal}",
            {},
        )

    prev_hist = df[hist_col].iloc[-2]
    curr_hist = df[hist_col].iloc[-1]

    indicators = {
        "macd_hist": float(round(curr_hist, 6)),
        "price":     float(round(df["close"].iloc[-1], 4)),
    }

    if prev_hist < 0 and curr_hist >= 0:
        return StrategyResult(Signal.BUY,  0.7, "MACD histogram crossed above zero", indicators)
    if prev_hist > 0 and curr_hist <= 0:
        return StrategyResult(Signal.SELL, 0.7, "MACD histogram crossed below zero", indicators)
    return StrategyResult(Signal.HOLD, 0.0, "MACD no crossover", indicators)


# ── Router ────────────────────────────────────────────────────────────────────

STRATEGY_MAP = {
    "sma_crossover": sma_crossover,
    "rsi":           rsi_strategy,
    "macd":          macd_strategy,
}


def run_strategy(strategy_type: str, df: pd.DataFrame, params_json: str) -> StrategyResult:
    params = json.loads(params_json)
    fn = STRATEGY_MAP.get(strategy_type)
    if fn is None:
        raise ValueError(f"Unknown strategy: {strategy_type}")
    return fn(df, params)
