"""Trade accounting: new entries must never be conflated with trims/exits.

Guards the 2026-08-24 bug where auto_execute_scan_signals()'s MIXED return list
(conviction-drop sells followed by new buys) was reported wholesale as new
positions — the daily scan logged "opened 5 new positions" for 4 buys + 1 trim,
and the evening report printed trims as "NEW PAPER POSITION".
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.paper_trading import PaperTrade, count_new_buys, split_executed


def trade(ticker, action):
    return PaperTrade(date="2026-08-24", ticker=ticker, action=action, shares=1.0,
                      price=100.0, value=100.0, conviction=9.0,
                      reason="test", portfolio_value_after=1000.0)


class SplitExecuted(unittest.TestCase):

    def test_real_2026_08_24_batch(self):
        """The exact batch that produced the wrong count: 1 trim, then 4 buys."""
        batch = [trade("AVGO", "SELL_TRIM"), trade("LLY", "BUY"),
                 trade("XOM", "BUY"), trade("OXY", "BUY"), trade("MDT", "BUY")]
        buys, sells = split_executed(batch)
        self.assertEqual(count_new_buys(batch), 4)          # was reported as 5
        self.assertEqual([t.ticker for t in buys], ["LLY", "XOM", "OXY", "MDT"])
        self.assertEqual([t.action for t in sells], ["SELL_TRIM"])

    def test_count_excludes_every_sell_action(self):
        for action in ("SELL_TRIM", "SELL_FULL", "SELL_STOP"):
            with self.subTest(action=action):
                self.assertEqual(count_new_buys([trade("X", action)]), 0)

    def test_all_buys(self):
        batch = [trade("A", "BUY"), trade("B", "BUY")]
        buys, sells = split_executed(batch)
        self.assertEqual(len(buys), 2)
        self.assertEqual(sells, [])
        self.assertEqual(count_new_buys(batch), 2)

    def test_all_sells(self):
        batch = [trade("A", "SELL_FULL"), trade("B", "SELL_STOP")]
        buys, sells = split_executed(batch)
        self.assertEqual(buys, [])
        self.assertEqual(len(sells), 2)
        self.assertEqual(count_new_buys(batch), 0)

    def test_empty(self):
        self.assertEqual(split_executed([]), ([], []))
        self.assertEqual(count_new_buys([]), 0)

    def test_split_partitions_without_loss(self):
        batch = [trade("A", "BUY"), trade("B", "SELL_TRIM"), trade("C", "BUY")]
        buys, sells = split_executed(batch)
        self.assertEqual(len(buys) + len(sells), len(batch))


if __name__ == "__main__":
    unittest.main()
