"""
Universe Refresh Workflow [GAP 14]

Fetches S&P 500 + Nasdaq 100 constituents, applies liquidity filters,
and saves to data/universe_cache.json (valid for 7 days).

Run weekly (or force-refresh with --force):
  python workflows/refresh_universe.py
  python workflows/refresh_universe.py --force
  python workflows/refresh_universe.py --stats   # just show current cache stats
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from core.universe import build_universe, universe_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Refresh EquityLens stock universe")
    parser.add_argument("--force", action="store_true", help="Force refresh even if cache is fresh")
    parser.add_argument("--stats", action="store_true", help="Show current cache stats and exit")
    parser.add_argument("--min-cap", type=float, default=settings.min_market_cap,
                        help=f"Min market cap in USD (default: {settings.min_market_cap:,.0f})")
    parser.add_argument("--min-volume", type=float, default=settings.min_daily_volume,
                        help=f"Min daily dollar volume (default: {settings.min_daily_volume:,.0f})")
    args = parser.parse_args()

    if args.stats:
        stats = universe_stats()
        if stats["status"] == "no_cache":
            print("No universe cache found. Run without --stats to build it.")
        else:
            print(f"\nUniverse cache ({stats['fetched_date']}): {stats['count']} tickers")
            print("\nSector breakdown:")
            for sector, count in stats["sector_breakdown"].items():
                bar = "█" * (count // 3)
                print(f"  {sector:20} {count:4}  {bar}")
        return

    logger.info(
        f"Building universe (min_cap=${args.min_cap:,.0f}, min_vol=${args.min_volume:,.0f})..."
    )
    logger.info("This takes ~10-15 minutes for the full S&P 500 + Nasdaq 100 — grab a coffee.")

    universe = build_universe(
        min_market_cap=args.min_cap,
        min_daily_volume=args.min_volume,
        force_refresh=args.force,
    )

    print(f"\nUniverse ready: {len(universe)} tickers")
    stats = universe_stats()
    print("\nSector breakdown:")
    for sector, count in stats.get("sector_breakdown", {}).items():
        bar = "█" * (count // 3)
        print(f"  {sector:20} {count:4}  {bar}")

    print(f"\nCache saved to data/universe_cache.json (valid {7} days)")
    print("Run 'python workflows/daily_scan.py' to use the full universe.")


if __name__ == "__main__":
    main()
