"""
Persistence Layer — conviction history, portfolio positions, scan snapshots.

Local storage (always): data/conviction_history.json, data/portfolio.json
Supabase sync (optional): set SUPABASE_URL + SUPABASE_KEY in environment.

Enables:
  - Conviction drop matrix (needs yesterday's conviction per ticker)
  - Portfolio drift detection (needs entry prices + dates)
  - Dashboard history charts
"""
import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_DATA_DIR = "data"
_CONVICTION_HISTORY_FILE = os.path.join(_DATA_DIR, "conviction_history.json")
_PORTFOLIO_FILE = os.path.join(_DATA_DIR, "portfolio.json")

# Max days of conviction history to retain per ticker
_HISTORY_DAYS = 60


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class ConvictionSnapshot:
    ticker: str
    date: str          # ISO date YYYY-MM-DD
    conviction: float
    signal: str        # BUY | WATCHLIST | AVOID
    hunter_score: float
    data_confidence: float


@dataclass
class PortfolioPosition:
    ticker: str
    entry_date: str         # ISO date
    entry_price: float
    current_price: float
    shares: float
    position_pct: float     # fraction of portfolio
    stop_tier1: Optional[float]
    stop_tier2: Optional[float]
    stop_tier3: Optional[float]
    last_conviction: float
    notes: str = ""


# ── Conviction history ────────────────────────────────────────────────────────

def load_conviction_history() -> dict[str, list[dict]]:
    """Returns {ticker: [{date, conviction, signal, ...}, ...]} sorted oldest→newest."""
    if not os.path.exists(_CONVICTION_HISTORY_FILE):
        return {}
    try:
        with open(_CONVICTION_HISTORY_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"conviction_history load error: {e}")
        return {}


def save_conviction_history(history: dict[str, list[dict]]) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_CONVICTION_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def record_convictions(snapshots: list[ConvictionSnapshot]) -> None:
    """Append today's conviction scores. Prunes entries older than _HISTORY_DAYS."""
    history = load_conviction_history()
    cutoff = (datetime.date.today() - datetime.timedelta(days=_HISTORY_DAYS)).isoformat()

    for snap in snapshots:
        entries = history.setdefault(snap.ticker, [])
        # Remove today's entry if it already exists (idempotent re-runs)
        entries = [e for e in entries if e["date"] != snap.date]
        # Prune old entries
        entries = [e for e in entries if e["date"] >= cutoff]
        entries.append(asdict(snap))
        entries.sort(key=lambda e: e["date"])
        history[snap.ticker] = entries

    save_conviction_history(history)
    logger.info(f"Conviction history: recorded {len(snapshots)} tickers")


def get_previous_convictions(
    tickers: list[str],
    as_of: Optional[datetime.date] = None,
) -> dict[str, float]:
    """
    Returns the most recent conviction score BEFORE as_of (defaults to today)
    for each ticker. Used to feed the conviction drop matrix.
    """
    as_of = as_of or datetime.date.today()
    as_of_str = as_of.isoformat()
    history = load_conviction_history()
    result: dict[str, float] = {}

    for ticker in tickers:
        entries = history.get(ticker, [])
        # Find most recent entry strictly before as_of
        prior = [e for e in entries if e["date"] < as_of_str]
        if prior:
            result[ticker] = prior[-1]["conviction"]

    return result


def get_conviction_series(ticker: str, days: int = 30) -> list[dict]:
    """Returns the last `days` conviction snapshots for a ticker (for dashboard charts)."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    history = load_conviction_history()
    return [e for e in history.get(ticker, []) if e["date"] >= cutoff]


# ── Portfolio positions ───────────────────────────────────────────────────────

def load_portfolio() -> dict[str, dict]:
    """Returns {ticker: {entry_date, entry_price, shares, ...}}."""
    if not os.path.exists(_PORTFOLIO_FILE):
        return {}
    try:
        with open(_PORTFOLIO_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"portfolio load error: {e}")
        return {}


def save_portfolio(positions: dict[str, dict]) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_PORTFOLIO_FILE, "w") as f:
        json.dump(positions, f, indent=2)


def add_position(pos: PortfolioPosition) -> None:
    positions = load_portfolio()
    positions[pos.ticker] = asdict(pos)
    save_portfolio(positions)
    logger.info(f"Portfolio: added {pos.ticker} @ ${pos.entry_price:.2f}")


def update_position(ticker: str, **kwargs) -> None:
    """Update fields on an existing position (e.g. current_price, stop levels)."""
    positions = load_portfolio()
    if ticker not in positions:
        logger.warning(f"update_position: {ticker} not in portfolio")
        return
    positions[ticker].update(kwargs)
    save_portfolio(positions)


def remove_position(ticker: str) -> None:
    positions = load_portfolio()
    if ticker in positions:
        del positions[ticker]
        save_portfolio(positions)
        logger.info(f"Portfolio: removed {ticker}")


def get_held_tickers() -> list[str]:
    return list(load_portfolio().keys())


# ── Scan snapshot (for dashboard history) ────────────────────────────────────

def save_scan_snapshot(results: list[dict]) -> None:
    """
    Persists today's full scan as a dated JSON file.
    Dashboard uses this to render the results table.
    """
    os.makedirs(_DATA_DIR, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    path = os.path.join(_DATA_DIR, f"scan_{date_str}.json")
    with open(path, "w") as f:
        json.dump({
            "date": date_str,
            "generated_at": datetime.datetime.now().isoformat(),
            "results": results,
        }, f, indent=2)
    logger.info(f"Scan snapshot saved to {path}")


def load_scan_history(days: int = 7) -> list[dict]:
    """Load scan snapshots from the last N days (newest first)."""
    snapshots = []
    for i in range(days):
        date = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
        path = os.path.join(_DATA_DIR, f"scan_{date}.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    snapshots.append(json.load(f))
            except Exception:
                pass
    return snapshots


# ── Supabase sync (optional) ─────────────────────────────────────────────────

def _supabase_client():
    """Returns a Supabase client if credentials are configured, else None."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except ImportError:
        logger.debug("supabase-py not installed — skipping Supabase sync")
        return None
    except Exception as e:
        logger.warning(f"Supabase init error: {e}")
        return None


def sync_convictions_to_supabase(snapshots: list[ConvictionSnapshot]) -> None:
    """
    Upserts conviction snapshots to Supabase table `conviction_history`.
    Table schema: ticker TEXT, date DATE, conviction FLOAT, signal TEXT,
                  hunter_score FLOAT, data_confidence FLOAT
    Primary key: (ticker, date)
    No-op if Supabase is not configured.
    """
    sb = _supabase_client()
    if sb is None:
        return
    rows = [asdict(s) for s in snapshots]
    try:
        sb.table("conviction_history").upsert(rows).execute()
        logger.info(f"Supabase: synced {len(rows)} conviction records")
    except Exception as e:
        logger.warning(f"Supabase conviction sync error: {e}")


def sync_portfolio_to_supabase(positions: dict[str, dict]) -> None:
    """
    Upserts portfolio positions to Supabase table `portfolio_positions`.
    Table schema mirrors PortfolioPosition fields. Primary key: ticker.
    No-op if Supabase is not configured.
    """
    sb = _supabase_client()
    if sb is None:
        return
    rows = list(positions.values())
    if not rows:
        return
    try:
        sb.table("portfolio_positions").upsert(rows).execute()
        logger.info(f"Supabase: synced {len(rows)} portfolio positions")
    except Exception as e:
        logger.warning(f"Supabase portfolio sync error: {e}")
