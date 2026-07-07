"""
Backtest Workflow [GAP 9]

Two modes:

  Signal replay (default):
    Reads stored scan files and measures how BUY/WATCHLIST/AVOID signals
    performed over the following 5/10/20/60 days.
    Best after several days of daily scans have accumulated.

    python workflows/run_backtest.py

  Historical scan:
    Re-runs the scoring pipeline on a past date using yfinance OHLCV data.
    Good for cold-start validation before any scan history exists.

    python workflows/run_backtest.py --historical 2024-06-01

"""
import argparse
import datetime
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.backtest import BacktestConfig, compare_to_baseline, run_historical_scan, run_signal_replay
from workflows.daily_scan import DEFAULT_WATCHLIST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="EquityLens Backtest")
    parser.add_argument(
        "--historical",
        metavar="DATE",
        help="Run historical scan as of YYYY-MM-DD instead of signal replay",
    )
    parser.add_argument(
        "--hold-days",
        default="5,10,20,60",
        help="Comma-separated holding periods in calendar days (default: 5,10,20,60)",
    )
    parser.add_argument(
        "--signals",
        default="BUY,WATCHLIST,AVOID",
        help="Which signals to include (default: BUY,WATCHLIST,AVOID)",
    )
    parser.add_argument(
        "--min-conviction",
        type=float,
        default=0.0,
        help="Minimum conviction to include in backtest (default: 0)",
    )
    args = parser.parse_args()

    hold_days = [int(d.strip()) for d in args.hold_days.split(",")]
    signals = [s.strip() for s in args.signals.split(",")]

    config = BacktestConfig(
        hold_days=hold_days,
        signals_to_include=signals,
        min_conviction=args.min_conviction,
    )

    if args.historical:
        try:
            as_of = datetime.date.fromisoformat(args.historical)
        except ValueError:
            logger.error(f"Invalid date: {args.historical}. Use YYYY-MM-DD.")
            sys.exit(1)

        days_back = (datetime.date.today() - as_of).days
        if days_back < max(hold_days):
            logger.warning(
                f"as_of_date is only {days_back}d ago — need at least {max(hold_days)}d "
                "for full forward-return analysis. Results will be partial."
            )

        logger.info(f"Running historical scan as of {as_of}...")
        report = run_historical_scan(
            tickers=DEFAULT_WATCHLIST,
            as_of_date=as_of,
            config=config,
        )
    else:
        logger.info("Running signal-replay backtest on stored scan files...")
        report = run_signal_replay(config=config, save_report=True)

    print("\n" + "=" * 60)
    print(report.summary())
    print("=" * 60)

    # GAP 16: baseline comparison
    if report.total_signals >= 5:
        print("\nComparing BUY signals to SPY baseline...")
        baseline = compare_to_baseline(report)
        print("\n" + baseline.summary)
    elif report.total_signals == 0:
        print("\nNo signals found.")
        if not args.historical:
            print(
                "Tip: Run the daily scan for several days first to accumulate signal history.\n"
                "     Or use --historical 2024-01-15 for a cold-start backtest."
            )
    else:
        print(f"\n({report.total_signals} signals — need ≥5 BUY signals for baseline comparison)")


if __name__ == "__main__":
    main()
