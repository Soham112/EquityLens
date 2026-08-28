"""Backtest horizon maths + S/R detection.

Guards E22 (horizons silently clamped to the last available price, so a young
signal reported its return-to-date as a completed 60-day return) and the
_s1_support / _r1_resistance pair that E12's stop formula and E23's R/R replay
both depend on.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.backtest import _analyze_forward, _calendar_to_trading_days
from core.strategy_backtest import (EXIT_VARIANTS, PRODUCTION_EXIT_CFG,
                                    _r1_resistance, _s1_support)


class ProductionExitConfig(unittest.TestCase):
    """The backtest's idea of "production" must match what the book actually does.

    Until 2026-08-26 the EXIT_VARIANTS key labelled "live" was the PRE-E12 config,
    so run_all() reported superseded stops as current. Guards both the config
    itself and the naming convention that caused it.
    """

    def test_matches_e12_formula(self):
        """E12: S1-0.5xATR primary, 2.5xATR only as the no-S1 fallback.
        atr_mult=None is what makes _simulate skip the floor override."""
        self.assertTrue(PRODUCTION_EXIT_CFG["use_s1"])
        self.assertIsNone(PRODUCTION_EXIT_CFG["atr_mult"])
        self.assertTrue(PRODUCTION_EXIT_CFG["stall"])
        self.assertIsNone(PRODUCTION_EXIT_CFG["time_stop"])

    def test_exactly_one_variant_is_production(self):
        matching = [k for k, v in EXIT_VARIANTS.items() if v == PRODUCTION_EXIT_CFG]
        self.assertEqual(len(matching), 1)
        self.assertIn("PRODUCTION", matching[0])

    def test_no_variant_key_uses_a_status_word(self):
        """Keys describe CONFIG, never status — status words go stale silently."""
        for key in EXIT_VARIANTS:
            self.assertFalse(key.lower().startswith("live"), f"stale status label: {key}")


class CalendarToTradingDays(unittest.TestCase):
    """E22: an unreached horizon is MISSING DATA, never the last price."""

    def setUp(self):
        self.prices = [100.0 + i for i in range(50)]   # 50 bars

    def test_index_is_calendar_days_times_071(self):
        # 60 calendar days -> round(42.6) = index 43
        self.assertEqual(_calendar_to_trading_days(self.prices, 60), self.prices[43])
        self.assertEqual(_calendar_to_trading_days(self.prices, 5), self.prices[4])

    def test_unreached_horizon_returns_none_not_last_price(self):
        """The actual E22 bug: min(idx, len-1) clamped instead of returning None."""
        short = [100.0 + i for i in range(20)]         # index 43 unavailable
        self.assertIsNone(_calendar_to_trading_days(short, 60))
        # and must NOT be the final price, which is what the clamp returned
        self.assertNotEqual(_calendar_to_trading_days(short, 60), short[-1])

    def test_reached_horizons_still_resolve_on_short_series(self):
        short = [100.0 + i for i in range(20)]
        self.assertIsNotNone(_calendar_to_trading_days(short, 5))
        self.assertIsNotNone(_calendar_to_trading_days(short, 20))

    def test_empty_series(self):
        self.assertIsNone(_calendar_to_trading_days([], 5))

    def test_analyze_forward_propagates_none(self):
        short = [100.0 + i for i in range(20)]
        returns, *_ = _analyze_forward(100.0, None, short, [5, 10, 20, 60], 60)
        self.assertIsNone(returns[60])
        self.assertIsNotNone(returns[5])


class SupportResistanceDetection(unittest.TestCase):
    """_r1_resistance must mirror _s1_support exactly, on highs instead of lows."""

    def setUp(self):
        # two tested peaks (~120, ~140) and matching troughs
        highs, lows = [], []
        for peak, trough in [(120, 80), (120, 80), (140, 85), (140, 85)]:
            for j in range(15):
                v = trough + (peak - trough) * (1 - abs(j - 7) / 7.0)
                highs.append(v)
                lows.append(v - 2)
        self.highs = highs + [100.0] * 11
        self.lows = lows + [98.0] * 11
        self.i = len(self.highs) - 1

    def test_finds_nearest_tested_resistance_above(self):
        self.assertAlmostEqual(_r1_resistance(self.highs, self.i, 100.0), 120.0, places=1)

    def test_skips_resistance_below_entry(self):
        self.assertAlmostEqual(_r1_resistance(self.highs, self.i, 130.0), 140.0, places=1)

    def test_none_when_nothing_above(self):
        self.assertIsNone(_r1_resistance(self.highs, self.i, 200.0))

    def test_support_finds_level_below(self):
        s1 = _s1_support(self.lows, self.i, 100.0)
        self.assertIsNotNone(s1)
        self.assertLess(s1, 100.0)

    def test_none_when_nothing_below(self):
        self.assertIsNone(_s1_support(self.lows, self.i, 1.0))


if __name__ == "__main__":
    unittest.main()
