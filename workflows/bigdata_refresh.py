"""
BigData Refresh Workflow — populates data/bigdata_cache/{ticker}.json

Previously: required Claude + BigData MCP (subscription-based).
Now:        yfinance + Claude Haiku — fully automated, ~$0.03/week.

Sources:
  yfinance.info / .news / .earnings_history / .insider_transactions
  Claude Haiku → sentiment score, risk flags (litigation/SEC/auditor), narrative

SCHEDULE: Sunday only (equitylens-weekly-review task, Step 1)
MANUAL:   .venv/bin/python workflows/bigdata_refresh.py
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bigdata_client import CACHE_DIR, cache_age_days
from core.yfinance_sentiment import refresh_all, refresh_ticker

logger = logging.getLogger(__name__)


def get_weekly_tickers() -> list[str]:
    """This week's sentiment coverage: weekly universe + growth universe +
    discovery-admitted names. Growth/discovery names previously scanned with
    neutral sentiment and no insider data (coverage gap fixed 2026-07-10,
    ~+$0.03/week of Haiku)."""
    import glob
    files = sorted(glob.glob("data/weekly_universe_*.json"), reverse=True)
    if not files:
        logger.warning("No weekly universe file found — using fallback watchlist")
        return _fallback_tickers()
    with open(files[0]) as f:
        data = json.load(f)
    tickers = list(data.get("all_stocks", []))

    try:
        from core.growth_universe import get_growth_universe
        tickers += [t for t, _ in get_growth_universe()]
    except Exception as e:
        logger.warning(f"growth universe not added to sentiment refresh: {e}")
    try:
        admitted_path = Path("data/discovery_admitted.json")
        if admitted_path.exists():
            tickers += json.loads(admitted_path.read_text())
    except Exception:
        pass

    tickers = list(dict.fromkeys(tickers))
    logger.info(f"Sentiment refresh list: {len(tickers)} tickers "
                f"(weekly universe + growth + discovery-admitted)")
    return tickers


def _fallback_tickers() -> list[str]:
    from core.sector_map import MICRO_SECTORS, WILDCARD_POOL
    tickers = set(WILDCARD_POOL)
    for ms in MICRO_SECTORS.values():
        tickers.update(ms.get("candidates", []))
    return sorted(tickers)


def print_cache_status(tickers: list[str]) -> None:
    import datetime
    print(f"\n{'Ticker':<8} {'Status':<10} {'Age':<12} {'Source':<16} {'Sentiment'}")
    print("-" * 65)
    for ticker in tickers:
        cache_file = CACHE_DIR / f"{ticker.upper()}.json"
        if not cache_file.exists():
            print(f"{ticker:<8} {'MISSING':<10} {'—':<12} {'—':<16}")
            continue
        age = cache_age_days(ticker)
        age_str = f"{age}d" if age is not None else "?"
        with open(cache_file) as f:
            d = json.load(f)
        source = d.get("source", "bigdata")
        score = d.get("sentiment", {}).get("signals", {}).get("sentiment", {}).get("current", "?")
        status = "OK" if (age or 99) < 8 else "STALE"
        print(f"{ticker:<8} {status:<10} {age_str:<12} {source:<16} {score}")
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import argparse
    parser = argparse.ArgumentParser(description="Refresh BigData cache via yfinance + Haiku")
    parser.add_argument("tickers", nargs="*", help="Specific tickers (default: weekly universe)")
    parser.add_argument("--status", action="store_true", help="Show cache status only")
    args = parser.parse_args()

    tickers = args.tickers if args.tickers else get_weekly_tickers()

    if args.status:
        print_cache_status(tickers)
        sys.exit(0)

    print(f"\nRefreshing {len(tickers)} tickers via yfinance + Haiku...\n")
    result = refresh_all(tickers)
    print(f"\nDone: {len(result['success'])}/{result['total']} succeeded")
    if result["failed"]:
        print(f"Failed: {result['failed']}")
