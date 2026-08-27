"""Base counting (E21) and the Stage-3 distribution break (E19).

Both are pure given a price series — count_bases takes `closes` directly, and
check_distribution_break's yfinance call is replaced with a fake, so nothing
here touches the network.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

import core.momentum_monitor as mm
from core.screener import count_bases


def staircase(n_bases, base_days=40, leg_days=30, leg_gain=0.25, base_depth=0.12):
    """An advance with `n_bases` completed consolidations, each broken out of."""
    px, p = [], 100.0
    for _ in range(n_bases):
        for i in range(leg_days):
            p *= (1 + leg_gain / leg_days)
            px.append(p)
        peak = p
        for i in range(base_days):
            frac = i / base_days
            depth = base_depth * min(1.0, frac * 3)
            px.append(peak * (1 - depth * (1 - frac * 0.5)))
        px.append(peak * 1.02)          # breakout day completes the base
        p = peak * 1.02
    return pd.Series(px, index=pd.date_range("2024-01-01", periods=len(px), freq="B"))


class BaseCounter(unittest.TestCase):
    """E21: bases 4+ mark a late-stage, crowded trade and get downsized."""

    def test_counts_engineered_bases_exactly(self):
        for n in (1, 2, 4):
            with self.subTest(bases=n):
                self.assertEqual(count_bases("SYN", closes=staircase(n))["base_count"], n)

    def test_straight_advance_has_no_bases(self):
        s = pd.Series(np.linspace(100, 200, 300),
                      index=pd.date_range("2024-01-01", periods=300, freq="B"))
        self.assertEqual(count_bases("SYN", closes=s)["base_count"], 0)

    def test_late_stage_threshold(self):
        """4+ is the documented late-stage cut — guards the constant."""
        self.assertGreaterEqual(count_bases("SYN", closes=staircase(4))["base_count"], 4)
        self.assertLess(count_bases("SYN", closes=staircase(2))["base_count"], 4)

    def test_too_short_series_returns_none(self):
        s = pd.Series([100.0] * 10,
                      index=pd.date_range("2024-01-01", periods=10, freq="B"))
        self.assertIsNone(count_bases("SYN", closes=s))


class _FakeTicker:
    """Advance that reclaims MA200 then breaks -6% on 2.5x volume."""
    break_pct = 0.94
    break_vol = 2_500_000

    def __init__(self, ticker):
        pass

    def history(self, period=None):
        n = 300
        close = np.concatenate([
            np.full(150, 100.0) + np.random.RandomState(1).randn(150) * 0.5,
            np.linspace(100, 160, 150),
        ])
        close[-1] = close[-2] * self.break_pct
        vol = np.full(n, 1_000_000.0)
        vol[-1] = self.break_vol
        return pd.DataFrame(
            {"Close": close, "Volume": vol, "High": close * 1.01, "Low": close * 0.99},
            index=pd.date_range("2025-05-01", periods=n, freq="B"))


class DistributionBreak(unittest.TestCase):
    """E19: largest one-day decline of the advance, on overwhelming volume."""

    def setUp(self):
        self._orig = mm.yf.Ticker

    def tearDown(self):
        mm.yf.Ticker = self._orig

    def test_fires_on_record_decline_with_heavy_volume(self):
        mm.yf.Ticker = _FakeTicker
        fired, detail = mm.check_distribution_break("TEST")
        self.assertTrue(fired)
        self.assertIn("volume", detail.lower())

    def test_silent_on_low_volume(self):
        class LowVol(_FakeTicker):
            break_vol = 900_000
        mm.yf.Ticker = LowVol
        self.assertFalse(mm.check_distribution_break("TEST")[0])

    def test_silent_on_shallow_decline(self):
        """-1% is not a 'massive' break even on huge volume."""
        class Shallow(_FakeTicker):
            break_pct = 0.99
        mm.yf.Ticker = Shallow
        self.assertFalse(mm.check_distribution_break("TEST")[0])

    def test_silent_when_not_the_largest_decline(self):
        class NotLargest(_FakeTicker):
            def history(self, period=None):
                df = _FakeTicker("T").history(period)
                c = df["Close"].values.copy()
                c[220] = c[219] * 0.90      # a bigger earlier drop
                df["Close"] = c
                return df
        mm.yf.Ticker = NotLargest
        self.assertFalse(mm.check_distribution_break("TEST")[0])


if __name__ == "__main__":
    unittest.main()
