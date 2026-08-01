"""
Market stage classifier.

Classifies the current market into four broad stages:
1. Accumulation / sideways
2. Markup / bullish trend
3. Distribution / sideways after advance
4. Markdown / bearish trend
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import pandas_ta as ta


STAGE_DEFINITIONS = {
    1: {
        "name": "Mercado lateral (Acumulacion)",
        "bias": "neutral",
        "summary": "El precio se mueve sin una tendencia clara.",
        "recommendation": "Reducir agresividad y esperar ruptura confirmada.",
    },
    2: {
        "name": "Tendencia alcista (Markup)",
        "bias": "bullish",
        "summary": "El precio mantiene estructura alcista y medias alineadas.",
        "recommendation": "Priorizar compras validas por la estrategia y proteger con stop-loss.",
    },
    3: {
        "name": "Mercado lateral (Distribucion)",
        "bias": "caution",
        "summary": "El avance pierde fuerza y el precio empieza a oscilar en rango.",
        "recommendation": "Evitar nuevas entradas agresivas hasta confirmar continuidad o ruptura.",
    },
    4: {
        "name": "Tendencia bajista (Markdown)",
        "bias": "bearish",
        "summary": "El precio mantiene estructura bajista y medias deterioradas.",
        "recommendation": "Evitar compras long nuevas salvo reglas defensivas muy claras.",
    },
}


def classify_market_stage(
    candles: pd.DataFrame,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1d",
) -> dict[str, Any]:
    """Classify the latest market stage from OHLCV candles."""
    _validate_candles(candles)

    df = candles.copy().sort_values("timestamp").reset_index(drop=True)
    df["sma20"] = ta.sma(df["close"], length=20)
    df["sma50"] = ta.sma(df["close"], length=50)
    df["sma100"] = ta.sma(df["close"], length=100)
    df = df.dropna(subset=["sma20", "sma50", "sma100"]).reset_index(drop=True)

    if len(df) < 30:
        return _build_response(
            symbol=symbol,
            timeframe=timeframe,
            stage=1,
            confidence=35,
            indicators={"candles_count": len(candles)},
            evidence=[
                _evidence(
                    "Datos suficientes",
                    False,
                    "Se necesitan al menos 130 velas para clasificar con estabilidad.",
                )
            ],
            updated_at=_latest_timestamp(candles),
        )

    current = df.iloc[-1]
    previous = df.iloc[-6] if len(df) >= 6 else df.iloc[0]
    close = float(current["close"])
    sma20 = float(current["sma20"])
    sma50 = float(current["sma50"])
    sma100 = float(current["sma100"])

    recent = df.tail(20)
    earlier = df.iloc[-40:-20] if len(df) >= 40 else df.head(20)
    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())
    earlier_high = float(earlier["high"].max())
    earlier_low = float(earlier["low"].min())

    recent_return_pct = _pct_change(close, float(df.iloc[-21]["close"]))
    medium_return_pct = _pct_change(close, float(df.iloc[-61]["close"])) if len(df) >= 61 else recent_return_pct
    range_pct = ((recent_high - recent_low) / close) * 100 if close else 0.0
    distance_to_sma100_pct = _pct_change(close, sma100)
    sma20_slope_pct = _pct_change(sma20, float(previous["sma20"]))
    sma50_slope_pct = _pct_change(sma50, float(previous["sma50"]))
    sma100_slope_pct = _pct_change(sma100, float(previous["sma100"]))

    bullish_stack = sma20 > sma50 > sma100
    bearish_stack = sma20 < sma50 < sma100
    price_above_sma100 = close > sma100
    price_below_sma100 = close < sma100
    rising_mas = sma20_slope_pct > 0 and sma50_slope_pct > 0
    falling_mas = sma20_slope_pct < 0 and sma50_slope_pct < 0
    higher_structure = recent_high > earlier_high and recent_low > earlier_low
    lower_structure = recent_high < earlier_high and recent_low < earlier_low
    range_like = abs(sma50_slope_pct) < 1.0 and range_pct < 18.0
    after_advance = medium_return_pct > 8.0 and close >= sma100

    if bullish_stack and price_above_sma100 and (rising_mas or higher_structure):
        stage = 2
        confidence = _confidence(72, bullish_stack, price_above_sma100, rising_mas, higher_structure)
    elif bearish_stack and price_below_sma100 and (falling_mas or lower_structure):
        stage = 4
        confidence = _confidence(72, bearish_stack, price_below_sma100, falling_mas, lower_structure)
    elif after_advance and range_like:
        stage = 3
        confidence = _confidence(62, after_advance, range_like, not rising_mas, close >= sma50)
    else:
        stage = 1
        confidence = _confidence(55, range_like, not bullish_stack, not bearish_stack, abs(distance_to_sma100_pct) < 8.0)

    indicators = {
        "candles_count": len(candles),
        "price": _round(close),
        "sma20": _round(sma20),
        "sma50": _round(sma50),
        "sma100": _round(sma100),
        "sma20_slope_pct": _round(sma20_slope_pct),
        "sma50_slope_pct": _round(sma50_slope_pct),
        "sma100_slope_pct": _round(sma100_slope_pct),
        "range_pct": _round(range_pct),
        "distance_to_sma100_pct": _round(distance_to_sma100_pct),
        "recent_return_pct": _round(recent_return_pct),
        "medium_return_pct": _round(medium_return_pct),
    }

    evidence = [
        _evidence("Precio sobre SMA100", price_above_sma100, f"{_round(distance_to_sma100_pct)}% vs SMA100"),
        _evidence("SMA20 > SMA50 > SMA100", bullish_stack, "Alineacion alcista"),
        _evidence("SMA20 < SMA50 < SMA100", bearish_stack, "Alineacion bajista"),
        _evidence("Medias subiendo", rising_mas, f"SMA20 {_round(sma20_slope_pct)}%, SMA50 {_round(sma50_slope_pct)}%"),
        _evidence("Medias bajando", falling_mas, f"SMA20 {_round(sma20_slope_pct)}%, SMA50 {_round(sma50_slope_pct)}%"),
        _evidence("Estructura alcista", higher_structure, "Maximos y minimos recientes mas altos"),
        _evidence("Estructura bajista", lower_structure, "Maximos y minimos recientes mas bajos"),
        _evidence("Comportamiento lateral", range_like, f"Rango reciente {_round(range_pct)}%"),
    ]

    return _build_response(
        symbol=symbol,
        timeframe=timeframe,
        stage=stage,
        confidence=confidence,
        indicators=indicators,
        evidence=evidence,
        updated_at=_latest_timestamp(df),
    )


def _validate_candles(candles: pd.DataFrame) -> None:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(candles.columns)
    if missing:
        raise ValueError(f"Missing required candle columns: {sorted(missing)}")
    if candles.empty:
        raise ValueError("No candles available")


def _build_response(
    *,
    symbol: str,
    timeframe: str,
    stage: int,
    confidence: int,
    indicators: dict[str, Any],
    evidence: list[dict[str, Any]],
    updated_at: str | None,
) -> dict[str, Any]:
    definition = STAGE_DEFINITIONS[stage]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "stage": stage,
        "name": definition["name"],
        "bias": definition["bias"],
        "confidence": confidence,
        "summary": definition["summary"],
        "recommendation": definition["recommendation"],
        "indicators": indicators,
        "evidence": evidence,
        "updated_at": updated_at,
    }


def _evidence(label: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"label": label, "passed": passed, "detail": detail}


def _pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return ((current - previous) / previous) * 100


def _confidence(base: int, *checks: bool) -> int:
    score = base + sum(6 for check in checks if check)
    return min(score, 95)


def _round(value: float) -> float:
    return round(float(value), 4)


def _latest_timestamp(df: pd.DataFrame) -> str | None:
    if df.empty:
        return None
    return pd.to_datetime(df.iloc[-1]["timestamp"]).to_pydatetime().isoformat()
