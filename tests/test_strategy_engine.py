"""Unit tests for the strategy signal generators (sma_crossover, rsi_strategy, macd_strategy).

Each test builds a synthetic close-price series crafted so the underlying indicator
(SMA/RSI/MACD histogram) crosses its threshold on the very last candle — that's the
only bar these functions look at (`prev` vs `curr`). Values were derived by running
pandas_ta directly against candidate series and picking ones that land exactly where
we want, not by reasoning about the indicator math by hand.
"""
import pandas as pd
import pytest

from app.services.strategy_engine import Signal, macd_strategy, rsi_strategy, sma_crossover


def _df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


# ── SMA Crossover ───────────────────────────────────────────────────────────────

class TestSmaCrossover:
    def test_golden_cross_without_trend_filter_is_buy(self):
        # Fast SMA(3) crosses above slow SMA(5) on the last candle (steady decline
        # then a sharp bounce).
        closes = [50, 49, 48, 47, 46, 45, 44, 43, 42, 60]
        result = sma_crossover(_df(closes), {"fast": 3, "slow": 5, "trend_filter_enabled": False})
        assert result.signal == Signal.BUY
        assert "Golden cross" in result.reason

    def test_death_cross_without_trend_filter_is_sell(self):
        # Mirror image: steady rise then a sharp drop crosses fast SMA below slow SMA.
        closes = [42, 43, 44, 45, 46, 47, 48, 49, 50, 30]
        result = sma_crossover(_df(closes), {"fast": 3, "slow": 5, "trend_filter_enabled": False})
        assert result.signal == Signal.SELL
        assert "Death cross" in result.reason

    def test_no_crossover_is_hold(self):
        # Clean uptrend: fast SMA stays above slow SMA on both the prev and curr
        # candle, so there's nothing to cross.
        closes = list(range(1, 11))
        result = sma_crossover(_df(closes), {"fast": 3, "slow": 5, "trend_filter_enabled": False})
        assert result.signal == Signal.HOLD
        assert result.indicators["trend_bias"] == "bullish"

    def test_golden_cross_allowed_when_price_above_trend_sma(self):
        closes = [10] * 10 + [50, 49, 48, 47, 46, 45, 44, 43, 42, 60]
        result = sma_crossover(
            _df(closes), {"fast": 3, "slow": 5, "trend_filter_enabled": True, "trend_sma_period": 15}
        )
        assert result.signal == Signal.BUY
        assert result.indicators["trend_allows_entry"] is True

    def test_golden_cross_blocked_when_price_below_trend_sma(self):
        # Same crossover shape, but a high-value prefix drags the trend SMA above
        # the current price -> the trend filter should veto the entry.
        closes = [200] * 10 + [50, 49, 48, 47, 46, 45, 44, 43, 42, 60]
        result = sma_crossover(
            _df(closes), {"fast": 3, "slow": 5, "trend_filter_enabled": True, "trend_sma_period": 15}
        )
        assert result.signal == Signal.HOLD
        assert "skipped by trend filter" in result.reason
        assert result.indicators["trend_allows_entry"] is False

    def test_not_enough_data_is_hold_with_zero_confidence(self):
        result = sma_crossover(_df([100.0]), {"fast": 3, "slow": 5, "trend_filter_enabled": False})
        assert result.signal == Signal.HOLD
        assert result.confidence == 0.0


# ── RSI Strategy ─────────────────────────────────────────────────────────────────

class TestRsiStrategy:
    def test_recovering_from_oversold_is_buy(self):
        # 15 bars of steady decline push RSI under 30, then a bounce brings it back
        # above the threshold on the last candle.
        closes = [100 - i for i in range(1, 16)] + [95]
        result = rsi_strategy(_df(closes), {"period": 14, "oversold": 30, "overbought": 70})
        assert result.signal == Signal.BUY
        assert "oversold" in result.reason

    def test_leaving_overbought_is_sell(self):
        closes = [100 + i for i in range(1, 16)] + [116, 110]
        result = rsi_strategy(_df(closes), {"period": 14, "oversold": 30, "overbought": 70})
        assert result.signal == Signal.SELL
        assert "overbought" in result.reason

    def test_no_threshold_cross_is_hold(self):
        closes = [100, 101] * 8
        result = rsi_strategy(_df(closes), {"period": 14, "oversold": 30, "overbought": 70})
        assert result.signal == Signal.HOLD
        assert "neutral" in result.reason

    def test_not_enough_data_is_hold(self):
        result = rsi_strategy(_df([100.0]), {"period": 14})
        assert result.signal == Signal.HOLD
        assert result.confidence == 0.0


# ── MACD Strategy ────────────────────────────────────────────────────────────────

class TestMacdStrategy:
    def test_histogram_crosses_above_zero_is_buy(self):
        # Accelerating decline (histogram settles negative) followed by a rally
        # that flips the histogram positive on the last candle.
        declines = [100 - (i ** 1.4) * 0.3 for i in range(45)]
        rally = [declines[-1] * (1.03 ** i) for i in range(1, 4)]
        closes = declines + rally
        result = macd_strategy(_df(closes), {"fast": 12, "slow": 26, "signal": 9})
        assert result.signal == Signal.BUY
        assert "above zero" in result.reason

    def test_histogram_crosses_below_zero_is_sell(self):
        rises = [100 + (i ** 1.4) * 0.3 for i in range(45)]
        drop = [rises[-1] * (0.97 ** i) for i in range(1, 3)]
        closes = rises + drop
        result = macd_strategy(_df(closes), {"fast": 12, "slow": 26, "signal": 9})
        assert result.signal == Signal.SELL
        assert "below zero" in result.reason

    def test_flat_series_is_hold(self):
        closes = [100.0] * 40
        result = macd_strategy(_df(closes), {"fast": 12, "slow": 26, "signal": 9})
        assert result.signal == Signal.HOLD
        assert result.indicators["macd_hist"] == 0.0

    def test_not_enough_data_is_hold(self):
        # NOTE: unlike sma_crossover/rsi_strategy, macd_strategy only degrades
        # gracefully once pandas_ta has emitted the MACDh column at all (34+ rows
        # for fast=12/slow=26/signal=9). Fewer rows raise a KeyError instead of
        # returning HOLD — see the bug flagged separately for a real fix.
        result = macd_strategy(_df([100.0] * 34), {"fast": 12, "slow": 26, "signal": 9})
        assert result.signal == Signal.HOLD
        assert result.confidence == 0.0
