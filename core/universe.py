"""
Universe Builder [GAP 14]

Sources:
  S&P 500  — Wikipedia (free, ~500 tickers)
  Nasdaq 100 — Wikipedia (free, ~100 tickers)

Pipeline:
  1. Fetch constituent lists → deduplicate
  2. Filter by liquidity: market_cap >= min_market_cap, avg_volume >= min_daily_volume
  3. Map yfinance industry → internal sector name
  4. Cache to data/universe_cache.json for CACHE_TTL_DAYS

Tiered scan assembly:
  Tier 1 (always scanned): high-conviction history + held positions, up to TIER1_SIZE
  Tier 2 (rotating): remaining universe split into day-buckets; one bucket per day
  build_scan_list() returns the combined list for today's run.
"""
import datetime
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 7
CACHE_PATH = os.path.join("data", "universe_cache.json")

TIER1_SIZE = 50       # always-scanned positions
TIER2_BUCKET_SIZE = 75  # additional tickers rotated in each day

# ── Sector mapping ────────────────────────────────────────────────────────────
# yfinance `info["industry"]` → internal sector name

_INDUSTRY_TO_SECTOR: dict[str, str] = {
    # Semiconductors
    "Semiconductors": "semiconductors",
    "Semiconductor Equipment & Materials": "semiconductors",

    # AI / cloud infrastructure
    "Computer Hardware": "ai_infrastructure",
    "Electronic Components": "ai_infrastructure",
    "Information Technology Services": "ai_infrastructure",

    # Cybersecurity
    "Software - Infrastructure": "cybersecurity",

    # Software / SaaS
    "Software - Application": "technology",
    "Internet Content & Information": "technology",
    "Communication Equipment": "technology",
    "Electronic Gaming & Multimedia": "technology",

    # Consumer tech
    "Consumer Electronics": "technology",
    "Computer & Technology": "technology",

    # Telecom
    "Telecom Services": "telecom",
    "Wireless Telecom": "telecom",

    # Financials
    "Banks - Diversified": "financials",
    "Banks - Regional": "financials",
    "Asset Management": "financials",
    "Insurance - Diversified": "financials",
    "Insurance - Life": "financials",
    "Capital Markets": "financials",
    "Financial Data & Stock Exchanges": "financials",
    "Credit Services": "financials",
    "Insurance - Property & Casualty": "financials",

    # Healthcare
    "Drug Manufacturers - General": "healthcare",
    "Drug Manufacturers - Specialty & Generic": "healthcare",
    "Biotechnology": "healthcare",
    "Medical Devices": "healthcare",
    "Healthcare Plans": "healthcare",
    "Medical Instruments & Supplies": "healthcare",
    "Health Information Services": "healthcare",
    "Diagnostics & Research": "healthcare",
    "Medical Distribution": "healthcare",
    "Pharmaceutical Retailers": "healthcare",

    # Energy
    "Oil & Gas Integrated": "energy",
    "Oil & Gas E&P": "energy",
    "Oil & Gas Midstream": "energy",
    "Oil & Gas Refining & Marketing": "energy",
    "Oil & Gas Equipment & Services": "energy",
    "Utilities - Regulated Electric": "energy",
    "Utilities - Diversified": "energy",
    "Utilities - Independent Power Producers": "energy",

    # Industrials
    "Aerospace & Defense": "industrials",
    "Industrial Distribution": "industrials",
    "Specialty Industrial Machinery": "industrials",
    "Engineering & Construction": "industrials",
    "Waste Management": "industrials",
    "Railroads": "industrials",
    "Trucking": "industrials",
    "Airlines": "industrials",
    "Integrated Freight & Logistics": "industrials",

    # Consumer
    "Discount Stores": "consumer",
    "Specialty Retail": "consumer",
    "Home Improvement Retail": "consumer",
    "Auto Manufacturers": "consumer",
    "Auto Parts": "consumer",
    "Restaurants": "consumer",
    "Packaged Foods": "consumer",
    "Beverages - Non-Alcoholic": "consumer",
    "Household & Personal Products": "consumer",
    "Apparel Retail": "consumer",
    "Department Stores": "consumer",

    # Real estate
    "REIT - Retail": "real_estate",
    "REIT - Office": "real_estate",
    "REIT - Residential": "real_estate",
    "REIT - Diversified": "real_estate",
    "REIT - Industrial": "real_estate",
    "Real Estate Services": "real_estate",
}

_SECTOR_FALLBACK: dict[str, str] = {
    "Technology": "technology",
    "Healthcare": "healthcare",
    "Financials": "financials",
    "Consumer Discretionary": "consumer",
    "Consumer Staples": "consumer",
    "Energy": "energy",
    "Industrials": "industrials",
    "Communication Services": "technology",
    "Utilities": "energy",
    "Real Estate": "real_estate",
    "Basic Materials": "materials",
}


def _industry_to_sector(industry: Optional[str], yf_sector: Optional[str]) -> str:
    if industry:
        mapped = _INDUSTRY_TO_SECTOR.get(industry)
        if mapped:
            return mapped
    if yf_sector:
        return _SECTOR_FALLBACK.get(yf_sector, "other")
    return "other"


# ── Constituent fetching ──────────────────────────────────────────────────────

# Hardcoded constituents — more reliable than scraping Wikipedia which rate-limits bots.
# S&P 500 large-caps + full Nasdaq 100. Updated July 2026 (verified against yfinance).
_SP500_TICKERS = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","BRK-B","TSLA","AVGO",
    "JPM","LLY","UNH","XOM","V","MA","COST","HD","PG","JNJ","ABBV","MRK","ORCL",
    "CVX","BAC","CRM","NFLX","KO","PEP","TMO","WMT","MCD","CSCO","ABT","IBM",
    "GE","AMD","NOW","INTU","TXN","ISRG","QCOM","CAT","RTX","GS","SPGI","BX",
    "HON","AMGN","BKNG","VRTX","DHR","LOW","C","AXP","PLD","CB","BSX",
    "SYK","ADI","GILD","AMAT","TJX","LRCX","MU","KLAC","REGN","PANW","CMG",
    "CI","BDX","SO","DUK","AON","MCO","ZTS","ITW","ETN","NOC","EMR","APH",
    "ICE","SHW","EQIX","PH","ECL","FCX","HCA","WM","FISV","SNPS","CDNS","CME",
    "MSI","HLT","MAR","COF","USB","PNC","TFC","WFC","AIG","ALL","TRV","MET",
    "PRU","LMT","GD","BA","UNP","CSX","NSC","CVS","ELV","HUM",
    "BIIB","IDXX","IQV","EW","STE","RMD","BAX","ZBH","HSY","GIS",
    "CPB","HRL","SJM","MKC","CLX","CL","KMB","CHD","SPG","O","AMT",
    "CCI","PSA","DLR","AVB","EQR","VTR","WELL","NEM","ALB","LIN","APD",
    "SLB","HAL","BKR","OXY","COP","EOG","DVN","APA",
    # Additional large-caps
    "UBER","ABNB","DASH","LYFT","SNAP","PINS","TWLO","U","RBLX","HOOD",
    "COIN","MSTR","XYZ","PYPL","AFRM","SOFI","NU","MPWR","ENPH","FSLR",
]

_NDX100_TICKERS = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","TSLA","AVGO","COST",
    "NFLX","TMUS","AMD","QCOM","INTU","AMAT","ISRG","TXN","MU","LRCX",
    "KLAC","SNPS","CDNS","MRVL","ADSK","REGN","VRTX","GILD","AMGN","PYPL",
    "ADP","CRWD","PANW","FTNT","MELI","PDD","KDP","MDLZ","MNST","PEP",
    "SBUX","CME","CTAS","FAST","PAYX","ODFL","VRSK","IDXX","DXCM",
    "EXC","XEL","AEP","WBD","CMCSA","NXPI","ASML","ARM","INTC","ON",
    "ROST","DLTR","ORLY","PCAR","BIIB","ILMN","ALGN","CPRT",
    "CSX","ZM","TEAM","OKTA","DDOG","ZS","NET","SNOW","PLTR","MDB",
    "CRM","NOW","WDAY","VEEV","TTD","RGEN","SMCI","VRT","ANET","FANG",
    "CEG","GEHC","GFS","LCID","RIVN","LI","NIO","XPEV",
]


def fetch_constituent_names() -> dict[str, str]:
    """
    Scrape ticker → company name from the same Wikipedia tables used for
    constituents (S&P 500 "Security" column, Nasdaq 100 "Company" column).
    Returns {} on total failure — callers must treat names as optional.
    """
    names: dict[str, str] = {}
    try:
        import requests
        import pandas as pd
        from io import StringIO
        import warnings
        warnings.filterwarnings("ignore", message="Unverified HTTPS")

        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"}, verify=False, timeout=15,
        )
        df = pd.read_html(StringIO(r.text))[0]
        if "Symbol" in df.columns and "Security" in df.columns:
            for sym, name in zip(df["Symbol"], df["Security"]):
                names[str(sym).replace(".", "-")] = str(name)
    except Exception as e:
        logger.warning(f"S&P 500 name scrape failed: {e}")

    try:
        import requests
        import pandas as pd
        from io import StringIO
        r = requests.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            headers={"User-Agent": "Mozilla/5.0"}, verify=False, timeout=15,
        )
        tables = pd.read_html(StringIO(r.text))
        for t in tables:
            tick_col = next((c for c in t.columns if isinstance(c, str) and ("ticker" in c.lower() or "symbol" in c.lower())), None)
            name_col = next((c for c in t.columns if isinstance(c, str) and "company" in c.lower()), None)
            if tick_col and name_col and len(t) >= 90:
                for sym, name in zip(t[tick_col], t[name_col]):
                    names.setdefault(str(sym).replace(".", "-"), str(name))
                break
    except Exception as e:
        logger.warning(f"Nasdaq 100 name scrape failed: {e}")

    logger.info(f"Constituent names scraped: {len(names)}")
    return names


def _fetch_sp500_tickers() -> list[str]:
    """Scrape current S&P 500 constituents from Wikipedia. Falls back to hardcoded list."""
    try:
        import requests
        import pandas as pd
        from io import StringIO
        import warnings
        warnings.filterwarnings("ignore", message="Unverified HTTPS")
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"},
            verify=False, timeout=15,
        )
        df = pd.read_html(StringIO(r.text))[0]
        tickers = [t.replace(".", "-") for t in df["Symbol"].tolist()]
        tickers = list(dict.fromkeys(tickers))
        if len(tickers) >= 400:
            logger.info(f"S&P 500: scraped {len(tickers)} tickers from Wikipedia")
            return tickers
        logger.warning(f"S&P 500 scrape returned only {len(tickers)} — falling back to hardcoded list")
    except Exception as e:
        logger.warning(f"S&P 500 Wikipedia scrape failed ({e}) — using hardcoded list")
    logger.info(f"S&P 500: using hardcoded list ({len(_SP500_TICKERS)} tickers)")
    return list(_SP500_TICKERS)


def _fetch_nasdaq100_tickers() -> list[str]:
    """Scrape current Nasdaq 100 constituents from Wikipedia. Falls back to hardcoded list."""
    try:
        import requests
        import pandas as pd
        from io import StringIO
        import warnings
        warnings.filterwarnings("ignore", message="Unverified HTTPS")
        r = requests.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            headers={"User-Agent": "Mozilla/5.0"},
            verify=False, timeout=15,
        )
        tables = pd.read_html(StringIO(r.text))
        # Find the table with a 'Ticker' or 'Symbol' column
        for t in tables:
            col = next((c for c in t.columns if isinstance(c, str) and ("ticker" in c.lower() or "symbol" in c.lower())), None)
            if col and len(t) >= 90:
                tickers = [str(x).replace(".", "-") for x in t[col].tolist()]
                tickers = list(dict.fromkeys(tickers))
                logger.info(f"Nasdaq 100: scraped {len(tickers)} tickers from Wikipedia")
                return tickers
        logger.warning("Nasdaq 100 scrape: no valid table found — falling back to hardcoded list")
    except Exception as e:
        logger.warning(f"Nasdaq 100 Wikipedia scrape failed ({e}) — using hardcoded list")
    logger.info(f"Nasdaq 100: using hardcoded list ({len(_NDX100_TICKERS)} tickers)")
    return list(_NDX100_TICKERS)


# ── Liquidity filter ──────────────────────────────────────────────────────────

def _passes_liquidity(ticker: str, min_market_cap: float, min_daily_volume: float) -> tuple[bool, str, str]:
    """
    Returns (passes, industry, yf_sector).
    Uses yfinance fast_info for market cap + volume (single HTTP request).
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        info = t.fast_info

        market_cap = getattr(info, "market_cap", None) or 0
        avg_volume = getattr(info, "three_month_average_volume", None) or 0
        # fast_info doesn't carry industry — fetch from info if needed
        if market_cap >= min_market_cap and avg_volume >= (min_daily_volume / 50):
            # Approximate: avg_volume in shares, min_daily_volume in dollars
            # Use price * volume as dollar volume proxy
            price = getattr(info, "last_price", None) or 1
            dollar_volume = avg_volume * price
            if dollar_volume >= min_daily_volume:
                slow_info = t.info
                industry = slow_info.get("industry", "")
                yf_sector = slow_info.get("sector", "")
                return True, industry, yf_sector
    except Exception as e:
        logger.debug(f"{ticker} liquidity check error: {e}")
    return False, "", ""


# ── Cache ─────────────────────────────────────────────────────────────────────

def _load_cache() -> Optional[dict]:
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
        fetched = datetime.date.fromisoformat(cache.get("fetched_date", "2000-01-01"))
        age = (datetime.date.today() - fetched).days
        if age <= CACHE_TTL_DAYS:
            return cache
        logger.info(f"Universe cache is {age}d old (TTL={CACHE_TTL_DAYS}d) — will refresh")
        return None
    except Exception:
        return None


def _save_cache(entries: list[dict]) -> None:
    os.makedirs("data", exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump({
            "fetched_date": datetime.date.today().isoformat(),
            "count": len(entries),
            "entries": entries,
        }, f, indent=2)
    logger.info(f"Universe cache saved: {len(entries)} tickers → {CACHE_PATH}")


# ── Public API ────────────────────────────────────────────────────────────────

def build_universe(
    min_market_cap: float = 100e6,
    min_daily_volume: float = 50e6,
    force_refresh: bool = False,
    rate_limit_secs: float = 0.15,
) -> list[tuple[str, str]]:
    """
    Returns [(ticker, sector), ...] for all universe members passing liquidity filters.
    Uses cache if fresh; fetches + filters otherwise.

    rate_limit_secs: pause between yfinance calls to avoid rate-limiting.
    """
    if not force_refresh:
        cache = _load_cache()
        if cache:
            entries = cache["entries"]
            logger.info(f"Universe loaded from cache: {len(entries)} tickers")
            return [(e["ticker"], e["sector"]) for e in entries]

    logger.info("Building universe from scratch (S&P 500 + Nasdaq 100)...")

    sp500 = _fetch_sp500_tickers()
    ndx100 = _fetch_nasdaq100_tickers()
    all_tickers = list(dict.fromkeys(sp500 + ndx100))  # deduplicate, preserve order
    logger.info(f"Raw universe: {len(all_tickers)} unique tickers")

    names = fetch_constituent_names()

    entries: list[dict] = []
    skipped = 0

    for i, ticker in enumerate(all_tickers):
        passes, industry, yf_sector = _passes_liquidity(ticker, min_market_cap, min_daily_volume)
        if passes:
            sector = _industry_to_sector(industry, yf_sector)
            entries.append({"ticker": ticker, "sector": sector, "industry": industry,
                            "name": names.get(ticker, "")})
        else:
            skipped += 1

        if (i + 1) % 50 == 0:
            logger.info(f"  Screened {i+1}/{len(all_tickers)} — {len(entries)} passed, {skipped} skipped")

        time.sleep(rate_limit_secs)

    logger.info(f"Universe built: {len(entries)} tickers passed liquidity filter ({skipped} skipped)")
    _save_cache(entries)
    return [(e["ticker"], e["sector"]) for e in entries]


def load_universe() -> list[tuple[str, str]]:
    """
    Load universe from cache. Returns empty list if cache is missing or stale.
    Call build_universe() first (or via the refresh workflow).
    """
    cache = _load_cache()
    if not cache:
        return []
    return [(e["ticker"], e["sector"]) for e in cache["entries"]]


def load_name_map() -> dict[str, str]:
    """
    Ticker → company name from the universe cache (populated by the Sunday
    refresh; missing/empty for tickers added before names were stored).
    Reads the cache file even past TTL — a week-old name is still a name.
    """
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
        return {
            e["ticker"]: e["name"]
            for e in cache.get("entries", [])
            if e.get("name")
        }
    except Exception:
        return {}


def universe_stats() -> dict:
    """Summary stats for the current universe cache."""
    cache = _load_cache()
    if not cache:
        return {"status": "no_cache", "count": 0}

    entries = cache["entries"]
    sector_counts: dict[str, int] = {}
    for e in entries:
        s = e.get("sector", "other")
        sector_counts[s] = sector_counts.get(s, 0) + 1

    return {
        "status": "ok",
        "fetched_date": cache.get("fetched_date"),
        "count": len(entries),
        "sector_breakdown": dict(sorted(sector_counts.items(), key=lambda x: -x[1])),
    }


# ── Tiered scan list ──────────────────────────────────────────────────────────

def build_scan_list(
    max_tickers: int = 125,
    tier1_size: int = TIER1_SIZE,
    tier2_bucket_size: int = TIER2_BUCKET_SIZE,
) -> list[tuple[str, str]]:
    """
    Assemble today's scan list from the universe using a two-tier system:

    Tier 1 (always): held positions + high recent conviction → up to tier1_size
    Tier 2 (rotate): remaining universe split into day-buckets; bucket index = day_of_year % n_buckets

    Returns deduplicated list capped at max_tickers.
    """
    from core.persistence import get_held_tickers, load_conviction_history

    universe = load_universe()
    if not universe:
        logger.warning("Universe cache empty — falling back to DEFAULT_WATCHLIST")
        from workflows.daily_scan import DEFAULT_WATCHLIST
        return DEFAULT_WATCHLIST[:max_tickers]

    universe_map = dict(universe)  # ticker → sector

    # ── Tier 1: held positions ──
    held = get_held_tickers()
    tier1: list[tuple[str, str]] = [(t, universe_map.get(t, "other")) for t in held if t in universe_map]

    # ── Tier 1: high-conviction recent history ──
    history = load_conviction_history()
    recent_high: list[tuple[str, float]] = []
    for ticker, snapshots in history.items():
        if not snapshots:
            continue
        latest = snapshots[-1]
        if latest.get("conviction", 0) >= 7.0:
            recent_high.append((ticker, latest["conviction"]))

    recent_high.sort(key=lambda x: -x[1])
    for ticker, _ in recent_high:
        if len(tier1) >= tier1_size:
            break
        if ticker not in universe_map:
            continue
        entry = (ticker, universe_map[ticker])
        if entry not in tier1:
            tier1.append(entry)

    # ── Tier 2: rotating bucket ──
    tier1_tickers = {t for t, _ in tier1}
    remaining = [(t, s) for t, s in universe if t not in tier1_tickers]

    day_of_year = datetime.date.today().timetuple().tm_yday
    n_buckets = max(1, len(remaining) // tier2_bucket_size)
    bucket_idx = day_of_year % n_buckets
    bucket_start = bucket_idx * tier2_bucket_size
    tier2 = remaining[bucket_start: bucket_start + tier2_bucket_size]

    combined = tier1 + tier2
    combined = combined[:max_tickers]

    logger.info(
        f"Scan list: {len(tier1)} Tier-1 + {len(tier2)} Tier-2 = {len(combined)} tickers "
        f"(universe size: {len(universe)}, bucket {bucket_idx}/{n_buckets})"
    )
    return combined
