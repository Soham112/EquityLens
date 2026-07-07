"""
Growth Universe Refresh — runs Sunday 7:30 AM.

Three jobs:
  1. VALIDATE existing tickers — check each against inclusion criteria via yfinance.
     Flag and remove any that no longer qualify (delisted, revenue declining, market cap too small).
  2. REPORT gaps — output which sectors are thin so the Claude scheduled task knows
     where to focus its web search for new additions.
  3. SECTOR RADAR — scan 30+ sector ETFs for unusual momentum (>25% in 60d).
     Cross-reference with main scanner and Growth Scout coverage.
     Flag any booming sector with thin/zero representation in either system.
     Output feeds both the main $2,000 system and Growth Scout with the same signal.
"""
import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.growth_universe import GROWTH_UNIVERSE, get_growth_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Inclusion thresholds
MIN_MARKET_CAP    = 300_000_000     # $300M
MAX_MARKET_CAP    = 10_000_000_000  # $10B
MIN_DAILY_DOLLAR_VOL = 3_000_000    # $3M avg daily
MIN_REVENUE_GROWTH   = 0.0          # >0% (negative = flag for removal)
MIN_GROSS_MARGIN     = 0.25         # 25% floor (some hardware plays allowed)


def _check_ticker(ticker: str, sector: str) -> dict:
    result = {
        "ticker": ticker,
        "sector": sector,
        "status": "OK",         # OK | FLAG | REMOVE
        "reason": "",
        "market_cap": None,
        "daily_dollar_vol": None,
        "revenue_growth_yoy": None,
        "gross_margin": None,
    }
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info or {}

        # Price check
        hist = stock.history(period="5d")
        if hist.empty:
            result["status"] = "REMOVE"
            result["reason"] = "No price data — possibly delisted"
            return result

        current_price = float(hist["Close"].iloc[-1])
        avg_volume    = float(hist["Volume"].mean())
        daily_dol_vol = current_price * avg_volume
        result["daily_dollar_vol"] = round(daily_dol_vol)

        if daily_dol_vol < 500_000:
            result["status"] = "REMOVE"
            result["reason"] = f"Daily volume ${daily_dol_vol/1e6:.2f}M — too illiquid"
            return result
        elif daily_dol_vol < MIN_DAILY_DOLLAR_VOL:
            result["status"] = "FLAG"
            result["reason"] = f"Daily volume ${daily_dol_vol/1e6:.2f}M — thin, watch"

        # Market cap
        mktcap = info.get("marketCap")
        result["market_cap"] = mktcap
        if mktcap:
            if mktcap < 150_000_000:
                result["status"] = "REMOVE"
                result["reason"] = f"Market cap ${mktcap/1e6:.0f}M — too small, liquidity risk"
                return result
            elif mktcap > MAX_MARKET_CAP:
                result["status"] = "FLAG"
                result["reason"] = f"Market cap ${mktcap/1e9:.1f}B — graduated to large-cap, consider moving to main scanner"

        # Revenue growth
        rev_growth = info.get("revenueGrowth")
        result["revenue_growth_yoy"] = rev_growth
        if rev_growth is not None and rev_growth < -0.20:
            result["status"] = "FLAG"
            if result["reason"]:
                result["reason"] += f" | Revenue declining {rev_growth:.0%} YoY"
            else:
                result["reason"] = f"Revenue declining {rev_growth:.0%} YoY — thesis at risk"

        # Gross margin
        gross_margin = info.get("grossMargins")
        result["gross_margin"] = gross_margin
        if gross_margin is not None and gross_margin < MIN_GROSS_MARGIN:
            if result["status"] != "REMOVE":
                result["status"] = "FLAG"
                existing = result["reason"] + " | " if result["reason"] else ""
                result["reason"] = f"{existing}Gross margin {gross_margin:.0%} — below 25% floor"

    except Exception as e:
        result["status"] = "FLAG"
        result["reason"] = f"Data fetch error: {e}"

    return result


# ── Sector Radar ─────────────────────────────────────────────────────────────

# Every sector we track with its ETF proxy.
# Covers both established sectors (main scanner) and emerging ones (Growth Scout).
RADAR_SECTORS: list[tuple[str, str, str]] = [
    # (sector_name, etf_ticker, which_system_covers_it)
    # system: "main" | "growth" | "both" | "neither"
    ("Semiconductors",       "SMH",   "both"),
    ("Technology",           "QQQ",   "main"),
    ("Software",             "IGV",   "main"),
    ("Cybersecurity",        "CIBR",  "growth"),
    ("Cloud",                "WCLD",  "growth"),
    ("AI / Robotics",        "BOTZ",  "neither"),
    ("Biotech",              "XBI",   "growth"),
    ("Genomics",             "ARKG",  "growth"),
    ("Healthcare",           "XLV",   "main"),
    ("Clean Energy",         "ICLN",  "growth"),
    ("Solar",                "TAN",   "neither"),
    ("Nuclear Energy",       "NLR",   "neither"),
    ("EV / Batteries",       "LIT",   "growth"),
    ("Space",                "UFO",   "growth"),
    ("Defense Tech",         "ITA",   "growth"),
    ("Financials",           "XLF",   "main"),
    ("Fintech",              "ARKF",  "growth"),
    ("Energy",               "XLE",   "main"),
    ("Uranium",              "URA",   "neither"),
    ("Copper / Materials",   "COPX",  "neither"),
    ("Industrials",          "XLI",   "main"),
    ("Consumer Discretionary","XLY",  "main"),
    ("Real Estate",          "XLRE",  "main"),
    ("Quantum Computing",    "QTUM",  "growth"),
    ("Data Centers",         "DTCR",  "neither"),
    ("Water",                "PHO",   "neither"),
    ("AgriTech",             "MOO",   "neither"),
    ("Digital Health",       "EDOC",  "neither"),
    ("Shipping / Logistics", "BOAT",  "neither"),
    ("Precious Metals",      "GDX",   "neither"),
]

# Momentum thresholds
BOOM_THRESHOLD   = 0.25   # >25% in 60d = strong momentum, flag for both systems
WATCH_THRESHOLD  = 0.12   # >12% in 60d = gaining, monitor


def _sector_radar(growth_sector_counts: dict[str, int]) -> list[dict]:
    """
    Check 60-day ETF returns for all tracked sectors.
    Flag sectors with strong momentum that are underrepresented in main or Growth Scout.
    Returns list of radar alerts sorted by ETF return descending.
    """
    alerts = []

    # Count main scanner sectors (approximation from universe.py sector labels)
    main_sectors_covered = {
        "technology", "semiconductors", "healthcare", "financials",
        "energy", "consumer", "industrials", "materials", "utilities",
        "real_estate", "communication_services", "software",
    }

    for sector_name, etf, coverage in RADAR_SECTORS:
        try:
            hist = yf.Ticker(etf).history(period="90d")
            if hist.empty or len(hist) < 60:
                continue
            ret_60d = float((hist["Close"].iloc[-1] - hist["Close"].iloc[-60]) / hist["Close"].iloc[-60])
            ret_20d = float((hist["Close"].iloc[-1] - hist["Close"].iloc[-20]) / hist["Close"].iloc[-20])

            if ret_60d < WATCH_THRESHOLD:
                continue  # not interesting enough to flag

            # Determine momentum tier
            if ret_60d >= BOOM_THRESHOLD:
                momentum = "BOOM"
            else:
                momentum = "GAINING"

            # Growth Scout coverage for this sector
            sector_key = sector_name.lower().replace(" / ", "_").replace(" ", "_")
            growth_count = growth_sector_counts.get(sector_key, 0)
            # Also check common aliases
            for alias in [sector_name.lower(), sector_name.lower().split("/")[0].strip()]:
                growth_count = max(growth_count, growth_sector_counts.get(alias, 0))

            # Build alert
            gaps = []
            if coverage in ("neither", "growth") and growth_count < 3:
                gaps.append(f"Growth Scout has {growth_count} ticker(s) here — add more")
            if coverage in ("neither", "main"):
                gaps.append("Main scanner has no dedicated coverage — flag for watchlist addition")
            if coverage == "neither" and momentum == "BOOM":
                gaps.append("NEITHER system covers this sector — highest priority for new additions")

            if gaps or momentum == "BOOM":
                alerts.append({
                    "sector": sector_name,
                    "etf": etf,
                    "return_60d": round(ret_60d, 4),
                    "return_20d": round(ret_20d, 4),
                    "momentum": momentum,
                    "current_coverage": coverage,
                    "growth_scout_tickers": growth_count,
                    "gaps": gaps,
                    "action": (
                        f"Search for {sector_name} small/mid-cap stocks with >20% revenue growth. "
                        f"ETF {etf} up {ret_60d:.0%} in 60 days."
                    ),
                })
        except Exception as e:
            logger.debug(f"Radar: {etf} fetch failed: {e}")

    alerts.sort(key=lambda a: -a["return_60d"])
    return alerts


def _sector_coverage(universe: list[tuple[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, sector in universe:
        counts[sector] = counts.get(sector, 0) + 1
    return counts


def run_refresh() -> dict:
    today = datetime.date.today().isoformat()
    universe = get_growth_universe()
    logger.info(f"Validating {len(universe)} tickers in Growth Universe...")

    results  = []
    to_flag  = []
    to_remove = []

    for ticker, sector in universe:
        r = _check_ticker(ticker, sector)
        results.append(r)
        if r["status"] == "REMOVE":
            to_remove.append(ticker)
            logger.warning(f"REMOVE {ticker}: {r['reason']}")
        elif r["status"] == "FLAG":
            to_flag.append(ticker)
            logger.info(f"FLAG   {ticker}: {r['reason']}")
        else:
            logger.info(f"OK     {ticker} ({sector})")

    # Sector coverage report — identify thin sectors
    sector_counts = _sector_coverage(universe)
    thin_sectors = {s: c for s, c in sector_counts.items() if c < 4}
    missing_sectors = [
        s for s in ["semiconductors", "biotech", "clean_energy", "cybersecurity",
                    "space", "genomics", "fintech", "industrials"]
        if s not in sector_counts
    ]

    # ── Sector Radar ──
    logger.info("Running sector radar across 30 ETFs...")
    radar_alerts = _sector_radar(sector_counts)
    boom_alerts  = [a for a in radar_alerts if a["momentum"] == "BOOM"]
    watch_alerts = [a for a in radar_alerts if a["momentum"] == "GAINING"]

    report = {
        "refresh_date": today,
        "total_checked": len(results),
        "ok_count": len([r for r in results if r["status"] == "OK"]),
        "flag_count": len(to_flag),
        "remove_count": len(to_remove),
        "flagged": to_flag,
        "to_remove": to_remove,
        "sector_coverage": sector_counts,
        "thin_sectors": thin_sectors,
        "missing_sectors": missing_sectors,
        "sector_radar": {
            "boom_sectors": boom_alerts,
            "gaining_sectors": watch_alerts,
            "total_scanned": len(RADAR_SECTORS),
        },
        "details": results,
    }

    # Save report
    Path("data").mkdir(exist_ok=True)
    out = Path("data") / f"growth_universe_refresh_{today}.json"
    out.write_text(json.dumps(report, indent=2))
    logger.info(f"Refresh report saved → {out}")

    print("\n" + "=" * 60)
    print(f"GROWTH UNIVERSE REFRESH — {today}")
    print("=" * 60)
    print(f"Checked: {len(results)} | OK: {report['ok_count']} | "
          f"Flag: {len(to_flag)} | Remove: {len(to_remove)}")
    if to_remove:
        print(f"\nREMOVE ({len(to_remove)}): {', '.join(to_remove)}")
    if to_flag:
        print(f"FLAG   ({len(to_flag)}): {', '.join(to_flag)}")
    if thin_sectors:
        print(f"\nThin sectors (need additions): {thin_sectors}")
    if missing_sectors:
        print(f"Missing sectors (zero coverage): {missing_sectors}")

    if radar_alerts:
        print(f"\n{'─'*60}")
        print("SECTOR RADAR")
        print(f"{'─'*60}")
        if boom_alerts:
            print(f"BOOM (>{BOOM_THRESHOLD:.0%} in 60d) — act on these:")
            for a in boom_alerts:
                coverage_note = f"Growth Scout: {a['growth_scout_tickers']} tickers"
                print(f"  {a['sector']:25} {a['etf']:6} +{a['return_60d']:.0%} 60d | {coverage_note}")
                for g in a["gaps"]:
                    print(f"    → {g}")
        if watch_alerts:
            print(f"\nGAINING (>{WATCH_THRESHOLD:.0%} in 60d) — monitor:")
            for a in watch_alerts:
                print(f"  {a['sector']:25} {a['etf']:6} +{a['return_60d']:.0%} 60d")
    print("=" * 60)

    return report


if __name__ == "__main__":
    run_refresh()
