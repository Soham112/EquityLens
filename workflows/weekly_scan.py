"""
Weekly Scan — runs every Sunday as part of the Sunday workflow.

Flow:
  1. BigData cache refresh (Claude fetches via MCP for all candidate tickers)
  2. Run sector funnel (macro → microsectors → candidate stocks)
  3. Save weekly universe to data/weekly_universe_{date}.json
  4. Print a clean summary for the dashboard to read

The daily scan reads data/weekly_universe_{date}.json to know
which stocks to score that week. If no weekly universe exists,
daily scan falls back to the wildcard pool only.

BigData cache is refreshed weekly here — not daily and not manually.
Daily scans read from Sunday's cache (valid 7 days for our purposes).
"""
import datetime
import json
import logging
import os
import sys

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _refresh_bigdata_cache(tickers: list[str]) -> None:
    """
    Refresh BigData.com MCP cache for given tickers.
    This runs via Claude at the start of every Sunday scan.
    Python cannot call MCP directly — this function is a no-op stub
    that signals Claude to run the refresh before continuing.
    The actual MCP calls happen in the scheduled task prompt.
    """
    cache_dir = "data/bigdata_cache"
    os.makedirs(cache_dir, exist_ok=True)
    logger.info(
        f"[BigData] Cache refresh needed for {len(tickers)} tickers. "
        f"Claude will fetch via MCP before sector scoring."
    )


def run_weekly_scan() -> dict:
    from agents.scout import run_weekly_funnel
    from core.sector_map import WILDCARD_POOL, MICRO_SECTORS

    today = datetime.date.today().isoformat()
    logger.info(f"=== Weekly scan starting — {today} ===")

    # Step 1: Signal that BigData cache should be refreshed this Sunday
    # (the scheduled task prompt instructs Claude to fetch MCP data first)
    all_tickers = list({t for ms in MICRO_SECTORS.values() for t in ms.get("stocks", [])})
    all_tickers += WILDCARD_POOL
    _refresh_bigdata_cache(all_tickers)

    # Step 2: Run sector funnel
    # E9 decision 2026-07-09: top-5 macro sectors (was top-3). Backtest evidence:
    # top-3 caught the eventual best sector only 36% of weeks, top-5 catches 51%.
    # Deep-scan universe grows ~60→~100 stocks — the price of that blind spot.
    universe = run_weekly_funnel(top_n_macro=5, top_n_micro=3)

    # Combine candidate stocks + wildcards (deduped)
    all_stocks = list(dict.fromkeys(universe.candidate_stocks + universe.wildcard_stocks))

    output = {
        "date": today,
        "top_macro_sectors": universe.top_macro_sectors,
        "top_microsectors": universe.top_microsectors,
        "candidate_stocks": universe.candidate_stocks,
        "wildcard_stocks": universe.wildcard_stocks,
        "all_stocks": all_stocks,
        "sector_scores": universe.sector_scores,
        "generated_at": datetime.datetime.now().isoformat(),
    }

    os.makedirs("data", exist_ok=True)
    path = f"data/weekly_universe_{today}.json"
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Weekly universe saved → {path}")

    # E9 Phase 2: log this week's full ranking under the live weights AND every
    # challenger formula, and score past weeks' forward returns. This is the live
    # shadow race that decides whether the weight change (equal thirds etc.)
    # graduates from backtest to production.
    try:
        from core.sector_backtest import log_weekly_ranking, score_ranking_log
        log_weekly_ranking()
        summary = score_ranking_log()
        if summary.get("weeks_scored"):
            logger.info(f"[E9-Phase2] Live formula race after {summary['weeks_scored']} scored weeks: "
                        + ", ".join(f"{k}={v:+.2%}" for k, v in summary["avg_fwd4w_by_formula"].items()))
    except Exception as e:
        logger.warning(f"[E9-Phase2] ranking log failed: {e}")

    _print_summary(output)
    return output


def _print_summary(output: dict):
    print("\n" + "=" * 60)
    print(f"  WEEKLY SCAN — {output['date']}")
    print("=" * 60)

    spy_row = None
    macro_scores = output["sector_scores"].get("macro", {})

    print("\nMACRO SECTORS")
    print(f"  {'Rank':<10} {'Sector':<26} {'Score':>6} {'20d':>7} {'60d':>7} {'vs SPY':>8}")
    print("  " + "-" * 68)
    for name, s in sorted(macro_scores.items(), key=lambda x: -x[1]["composite_score"]):
        rank  = s["rotation_rank"] or "—"
        r20   = f"{s['return_20d']:+.1%}" if s["return_20d"] is not None else "—"
        r60   = f"{s['return_60d']:+.1%}" if s["return_60d"] is not None else "—"
        vspy  = f"{s['vs_spy_60d']:+.1%}" if s["vs_spy_60d"] is not None else "—"
        score = s["composite_score"]
        print(f"  {rank:<10} {name:<26} {score:>6.1f} {r20:>7} {r60:>7} {vspy:>8}")

    print(f"\nTOP 3 MACRO: {', '.join(output['top_macro_sectors']).upper()}")

    micro_scores = output["sector_scores"].get("micro", {})
    for macro in output["top_macro_sectors"]:
        print(f"\n  ↳ {macro.upper()} — microsectors")
        relevant = {k: v for k, v in micro_scores.items() if v.get("macro") == macro}
        for name, s in sorted(relevant.items(), key=lambda x: -x[1]["composite_score"]):
            rank = s["rotation_rank"] or "—"
            r60  = f"{s['return_60d']:+.1%}" if s["return_60d"] is not None else "—"
            vspy = f"{s['vs_spy_60d']:+.1%}" if s["vs_spy_60d"] is not None else "—"
            top  = "★ " if name in output["top_microsectors"].get(macro, []) else "  "
            print(f"    {top}{rank:<10} {name:<22} 60d={r60:>7}  vs SPY={vspy:>8}")

    print(f"\nCANDIDATE STOCKS THIS WEEK ({len(output['candidate_stocks'])} from funnel + {len(output['wildcard_stocks'])} wildcards = {len(output['all_stocks'])} total)")
    # Print in rows of 8
    stocks = output["all_stocks"]
    for i in range(0, len(stocks), 8):
        print("  " + "  ".join(f"{s:<6}" for s in stocks[i:i+8]))

    print("\n" + "=" * 60)


def load_weekly_universe() -> dict | None:
    """
    Load the most recent weekly universe file.
    Called by daily_scan.py to get this week's stock list.
    Returns None if no file exists (daily scan uses wildcard pool only).
    """
    import glob
    files = sorted(glob.glob("data/weekly_universe_*.json"), reverse=True)
    if not files:
        return None
    with open(files[0]) as f:
        return json.load(f)


if __name__ == "__main__":
    run_weekly_scan()
