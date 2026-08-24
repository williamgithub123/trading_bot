"""Unit tests for the market-stage decision rules (_decide_stage)."""
from app.services.market_stage import _decide_stage


def _decide(**overrides):
    """All signals default to False/neutral; pass only what a scenario needs."""
    base = dict(
        bullish_stack=False,
        bearish_stack=False,
        price_above_sma100=False,
        price_below_sma100=False,
        rising_mas=False,
        falling_mas=False,
        higher_structure=False,
        lower_structure=False,
        after_advance=False,
        range_like=False,
        price_above_sma50=False,
        distance_to_sma100_within_8pct=False,
    )
    base.update(overrides)
    return _decide_stage(**base)


class TestStrongBullish:
    """Full SMA20 > SMA50 > SMA100 alignment: either signal alone is enough to confirm."""

    def test_full_stack_plus_rising_mas_is_stage_2(self):
        stage, confidence = _decide(
            bullish_stack=True, price_above_sma100=True, rising_mas=True,
        )
        assert stage == 2
        assert confidence == 72 + 6 * 3  # bullish_stack, price_above_sma100, rising_mas

    def test_full_stack_plus_higher_structure_is_stage_2(self):
        stage, confidence = _decide(
            bullish_stack=True, price_above_sma100=True, higher_structure=True,
        )
        assert stage == 2
        assert confidence == 72 + 6 * 3

    def test_full_stack_without_price_above_sma100_does_not_confirm(self):
        # bullish_stack alone isn't enough — price must also be above SMA100.
        stage, _ = _decide(bullish_stack=True, rising_mas=True, higher_structure=True)
        assert stage != 2


class TestSoftBullish:
    """Regression test: this is the exact case from the user's screenshot — price above
    SMA100, MAs rising, higher structure, but the SMA20>SMA50>SMA100 stack not aligned
    (e.g. a short pullback). Before the fix this fell through to stage 1."""

    def test_all_three_soft_signals_without_stack_is_stage_2(self):
        stage, confidence = _decide(
            bullish_stack=False,
            price_above_sma100=True,
            rising_mas=True,
            higher_structure=True,
        )
        assert stage == 2
        # checks = (bullish_stack=False, price_above_sma100=True, rising_mas=True, higher_structure=True)
        assert confidence == 60 + 6 * 3

    def test_missing_one_soft_signal_does_not_confirm(self):
        # price above SMA100 and rising MAs, but no higher structure -> not enough
        # corroboration without the full stack.
        stage, _ = _decide(price_above_sma100=True, rising_mas=True)
        assert stage != 2

    def test_missing_price_above_sma100_blocks_soft_bullish(self):
        stage, _ = _decide(rising_mas=True, higher_structure=True)
        assert stage != 2


class TestStrongBearish:
    def test_full_stack_plus_falling_mas_is_stage_4(self):
        stage, confidence = _decide(
            bearish_stack=True, price_below_sma100=True, falling_mas=True,
        )
        assert stage == 4
        assert confidence == 72 + 6 * 3

    def test_full_stack_plus_lower_structure_is_stage_4(self):
        stage, confidence = _decide(
            bearish_stack=True, price_below_sma100=True, lower_structure=True,
        )
        assert stage == 4
        assert confidence == 72 + 6 * 3


class TestSoftBearish:
    def test_all_three_soft_signals_without_stack_is_stage_4(self):
        stage, confidence = _decide(
            bearish_stack=False,
            price_below_sma100=True,
            falling_mas=True,
            lower_structure=True,
        )
        assert stage == 4
        # checks = (bearish_stack=False, price_below_sma100=True, falling_mas=True, lower_structure=True)
        assert confidence == 60 + 6 * 3

    def test_missing_one_soft_signal_does_not_confirm(self):
        stage, _ = _decide(price_below_sma100=True, falling_mas=True)
        assert stage != 4


class TestStage3Distribution:
    def test_after_advance_and_range_like_is_stage_3(self):
        # rising_mas=True and price_above_sma50=False set explicitly so the confidence
        # bonus is driven only by (after_advance, range_like) — the other two checks
        # in this branch are (not rising_mas) and (price_above_sma50).
        stage, confidence = _decide(
            after_advance=True, range_like=True, rising_mas=True, price_above_sma50=False,
        )
        assert stage == 3
        assert confidence == 62 + 6 * 2  # after_advance, range_like

    def test_after_advance_without_range_like_falls_to_stage_1(self):
        stage, _ = _decide(after_advance=True)
        assert stage == 1


class TestStage1Fallback:
    def test_no_signals_is_stage_1(self):
        stage, confidence = _decide()
        assert stage == 1
        # range_like=False, not bullish_stack=True, not bearish_stack=True, distance ok=False
        assert confidence == 55 + 6 * 2

    def test_conflicting_signals_fall_back_to_stage_1(self):
        # price above SMA100 but MAs not rising and no higher structure: not enough
        # for soft_bullish, and stack alone (without price_above_sma100) doesn't help.
        stage, _ = _decide(price_above_sma100=True)
        assert stage == 1
