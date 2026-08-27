"""Pipeline freshness maths.

Guards the detector added after the 2026-08-06..23 silent outage (17 days with
no scan while the dashboard rendered normally). Only the PURE helpers are tested
— pipeline_health() itself reads data/, which changes every scan, so asserting
against it would pass today and fail tomorrow for no useful reason.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pipeline_health import (FRESH, MISSING, STALE, _judge,
                                  _last_trading_day, _weekdays_between)


class WeekdayMaths(unittest.TestCase):

    def test_friday_to_monday_is_one_trading_day(self):
        """Weekend must not read as 3 days stale — this is why the check counts
        weekdays rather than calendar days."""
        self.assertEqual(_weekdays_between(datetime.date(2026, 8, 21),
                                           datetime.date(2026, 8, 24)), 1)

    def test_friday_to_saturday_is_zero(self):
        self.assertEqual(_weekdays_between(datetime.date(2026, 8, 21),
                                           datetime.date(2026, 8, 22)), 0)

    def test_same_day_is_zero(self):
        d = datetime.date(2026, 8, 24)
        self.assertEqual(_weekdays_between(d, d), 0)

    def test_backwards_range_is_zero_not_negative(self):
        self.assertEqual(_weekdays_between(datetime.date(2026, 8, 24),
                                           datetime.date(2026, 8, 21)), 0)

    def test_the_outage_span(self):
        """2026-08-06 (Thu) -> 2026-08-23 (Sun) = 11 weekdays."""
        self.assertEqual(_weekdays_between(datetime.date(2026, 8, 6),
                                           datetime.date(2026, 8, 23)), 11)

    def test_last_trading_day_rolls_back_over_weekend(self):
        # Sunday 2026-08-23 -> Friday 2026-08-21
        self.assertEqual(_last_trading_day(datetime.date(2026, 8, 23)),
                         datetime.date(2026, 8, 21))
        # a weekday is its own last trading day
        self.assertEqual(_last_trading_day(datetime.date(2026, 8, 25)),
                         datetime.date(2026, 8, 25))


class JudgeArtifact(unittest.TestCase):
    TODAY = datetime.date(2026, 8, 23)

    def test_flags_the_real_outage_as_stale(self):
        h = _judge("daily_scan", "weekday", datetime.date(2026, 8, 6), 1, self.TODAY)
        self.assertEqual(h.status, STALE)
        self.assertEqual(h.age_trading_days, 11)
        self.assertEqual(h.age_days, 17)

    def test_scan_from_last_trading_day_is_fresh(self):
        h = _judge("daily_scan", "weekday", datetime.date(2026, 8, 21), 1, self.TODAY)
        self.assertEqual(h.status, FRESH)

    def test_missing_when_no_date(self):
        h = _judge("daily_scan", "weekday", None, 1, self.TODAY)
        self.assertEqual(h.status, MISSING)
        self.assertIsNone(h.last_updated)

    def test_weekly_cadence_tolerates_a_week(self):
        """Sunday artifacts get 6 trading days before they count as stale."""
        h = _judge("weekly_universe", "Sunday", datetime.date(2026, 8, 16), 6, self.TODAY)
        self.assertEqual(h.status, FRESH)
        h2 = _judge("weekly_universe", "Sunday", datetime.date(2026, 8, 6), 6, self.TODAY)
        self.assertEqual(h2.status, STALE)

    def test_boundary_is_inclusive(self):
        """age == allowed is FRESH, one more is STALE.

        Dates chosen so the boundary is actually crossed: from Thu 2026-08-20 to
        Sun 2026-08-23 exactly one weekday (Fri 21st) elapses; from Wed 19th, two.
        (Fri 21st -> Sun 23rd is 0, which is why it cannot test this.)
        """
        h = _judge("x", "weekday", datetime.date(2026, 8, 20), 1, self.TODAY)
        self.assertEqual(h.age_trading_days, 1)
        self.assertEqual(h.status, FRESH)
        h2 = _judge("x", "weekday", datetime.date(2026, 8, 19), 1, self.TODAY)
        self.assertEqual(h2.age_trading_days, 2)
        self.assertEqual(h2.status, STALE)


if __name__ == "__main__":
    unittest.main()
