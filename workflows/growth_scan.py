"""
DEPRECATED — folded into workflows/daily_scan.py on 2026-07-05.

This used to run standalone at 9:40 AM (separate schedule from the main
9:35 AM daily_scan.py), scoring core/growth_universe.py with
agents/growth_hunter.py and calling core.growth_paper_trading.execute_buy()
directly. That bypassed every entry gate added to the swing pipeline
(earnings blackout, cross-track dedup, sector slots, risk-based sizing) —
two scorers, two schedules, one ungated portfolio.

The scoring logic still lives in agents/growth_hunter.py and
core/growth_universe.py, unchanged. It's now invoked from
core.screener.growth_hunter_candidates(), which converts results into the
same SwingSignal shape the 7-signal screener produces, so both sources flow
through core.growth_paper_trading.auto_enter_swing_signals() — one
portfolio, one chart-confirmation step, one set of gates — inside the
regular 9:35 AM daily_scan.py run.

The equitylens-growth-scan scheduled task has been disabled. This file is
kept only so a stray manual invocation doesn't error or double-execute;
it does nothing.
"""
import logging

logger = logging.getLogger(__name__)


def run_growth_scan() -> dict:
    logger.warning(
        "workflows/growth_scan.py is deprecated and does nothing — Growth Hunter "
        "candidates now run inside workflows/daily_scan.py's swing section. "
        "See core.screener.growth_hunter_candidates()."
    )
    return {"deprecated": True, "note": "Growth Hunter now runs inside daily_scan.py"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    run_growth_scan()
