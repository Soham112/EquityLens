"""
Pipeline freshness — is the data the dashboard is showing actually CURRENT?

NOT the same thing as core/staleness.py. That module scores how old one TICKER's
fundamental data is and penalises conviction accordingly (a per-stock scoring
input). This module asks a different question: has the PIPELINE itself run
recently, or is every screen quietly rendering output from weeks ago?

Why it exists: on 2026-08-23 the pipeline was found dead for 17 days (last scan
2026-08-06). Sentiment was 19 days stale, the universe cache 19 days old against
a 7-day TTL, and 8 live positions had gone unmonitored — yet the dashboard looked
completely normal throughout, because every loader globs for the newest file and
silently accepts whatever it finds. A missed run is recoverable; a missed run
nobody can SEE is what let it continue for two and a half weeks.

Design rule: never let a caller present stale output as current. Statuses are
FRESH / STALE / MISSING, and callers are expected to surface STALE loudly rather
than swallow it.

Holidays are not modelled — trading-day maths counts weekdays only, so the day
after a market holiday reads one day staler than it truly is. That errs toward
warning, which is the correct direction for a health check.
"""
from __future__ import annotations

import datetime
import glob
import json
import logging
import os
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = "data"

FRESH, STALE, MISSING = "FRESH", "STALE", "MISSING"


@dataclass
class ArtifactHealth:
    name: str
    status: str                  # FRESH | STALE | MISSING
    cadence: str                 # human-readable expected cadence
    last_updated: Optional[str]  # ISO date, None when MISSING
    age_days: Optional[int]      # calendar days
    age_trading_days: Optional[int]
    allowed_trading_days: int
    detail: str


def _weekdays_between(a: datetime.date, b: datetime.date) -> int:
    """Weekdays strictly after `a` up to and including `b` (0 if b <= a)."""
    if b <= a:
        return 0
    n = 0
    d = a
    while d < b:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _last_trading_day(today: Optional[datetime.date] = None) -> datetime.date:
    d = today or datetime.date.today()
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def _latest_dated_file(pattern: str, not_after: Optional[datetime.date] = None
                       ) -> tuple[Optional[str], Optional[datetime.date]]:
    """Newest file matching a `..._{YYYY-MM-DD}.json` pattern, with its date.

    `not_after` ignores files dated later than that day. Production never has
    future-dated files, but without this the check cannot be replayed against a
    past date (a later file would score as age 0 and mask the very gap being
    tested), so this is what makes the module verifiable.
    """
    files = sorted(glob.glob(os.path.join(DATA_DIR, pattern)), reverse=True)
    for f in files:
        stem = os.path.basename(f).rsplit(".", 1)[0]
        tail = stem.rsplit("_", 1)[-1]
        try:
            d = datetime.date.fromisoformat(tail)
        except ValueError:
            continue
        if not_after is not None and d > not_after:
            continue
        return f, d
    return None, None


def _judge(name: str, cadence: str, d: Optional[datetime.date],
           allowed_trading_days: int, today: datetime.date,
           missing_detail: str = "") -> ArtifactHealth:
    if d is None:
        return ArtifactHealth(name, MISSING, cadence, None, None, None,
                              allowed_trading_days,
                              missing_detail or "no file found")
    age_cal = (today - d).days
    age_td = _weekdays_between(d, today)
    ok = age_td <= allowed_trading_days
    return ArtifactHealth(
        name=name,
        status=FRESH if ok else STALE,
        cadence=cadence,
        last_updated=d.isoformat(),
        age_days=age_cal,
        age_trading_days=age_td,
        allowed_trading_days=allowed_trading_days,
        detail=(f"{age_td} trading day(s) old ({age_cal} calendar)"
                + ("" if ok else f" — expected within {allowed_trading_days}")),
    )


def _sentiment_cache_health(today: datetime.date) -> ArtifactHealth:
    """Sentiment refreshes Sunday; daily scans read it all week.

    Judged on the MEDIAN age of the tickers actually IN this week's refresh set
    (weekly universe + growth universe + discovery admits), not every file on
    disk. That distinction matters: the cache accumulates names from previous
    weeks whose sectors have since dropped out of the funnel, and those are
    correctly NOT refreshed — scoring them would report permanent staleness and
    train the reader to ignore the warning.
    """
    cache = os.path.join(DATA_DIR, "bigdata_cache")
    if not os.path.isdir(cache):
        return _judge("sentiment_cache", "Sunday", None, 6, today,
                      "no bigdata_cache/ directory")

    tickers: list[str] = []
    try:
        from workflows.bigdata_refresh import get_weekly_tickers
        tickers = get_weekly_tickers()
    except Exception as e:
        logger.debug(f"[PipelineHealth] refresh-set lookup failed: {e}")

    if tickers:
        paths = [os.path.join(cache, f"{t.upper()}.json") for t in tickers]
        paths = [p for p in paths if os.path.exists(p)]
        scope = f"{len(paths)}/{len(tickers)} of this week's refresh set"
    else:
        paths = glob.glob(os.path.join(cache, "*.json"))
        scope = f"{len(paths)} cached tickers (refresh set unavailable)"

    if not paths:
        return _judge("sentiment_cache", "Sunday", None, 6, today,
                      "refresh set has no cached files")

    dates: list[datetime.date] = []
    for p in paths:
        try:
            with open(p) as fh:
                d = json.load(fh)
            ts = d.get("fetched_at") or d.get("fetched_date")
            if ts:
                dates.append(datetime.date.fromisoformat(str(ts)[:10]))
        except Exception:
            continue
    if not dates:
        return _judge("sentiment_cache", "Sunday", None, 6, today,
                      "no fetched_at field in any cache file")
    dates.sort()
    median = dates[len(dates) // 2]
    h = _judge("sentiment_cache", "Sunday", median, 6, today)
    h.detail += f" — median of {scope}"
    return h


def _universe_cache_health(today: datetime.date) -> ArtifactHealth:
    p = os.path.join(DATA_DIR, "universe_cache.json")
    if not os.path.exists(p):
        return _judge("universe_cache", "Sunday (7d TTL)", None, 6, today,
                      "universe_cache.json missing")
    try:
        with open(p) as f:
            d = json.load(f)
        fetched = datetime.date.fromisoformat(d["fetched_date"])
    except Exception as e:
        return _judge("universe_cache", "Sunday (7d TTL)", None, 6, today,
                      f"unreadable: {e}")
    h = _judge("universe_cache", "Sunday (7d TTL)", fetched, 6, today)
    h.detail += f" — {d.get('count', '?')} tickers"
    return h


def pipeline_health(today: Optional[datetime.date] = None) -> dict:
    """Freshness of every artifact the dashboard renders from.

    Returns {status, stale, missing, checked_at, artifacts:[...]} where `status`
    is the WORST of the individual statuses — so a caller can gate on one field.
    """
    today = today or datetime.date.today()
    ltd = _last_trading_day(today)

    checks: list[ArtifactHealth] = []

    _, scan_d = _latest_dated_file("daily_scan_*.json", today)
    checks.append(_judge("daily_scan", "every weekday 9:35 AM", scan_d, 1, today,
                         "no daily_scan_*.json — pipeline has never run"))

    _, swing_d = _latest_dated_file("swing_candidates_*.json", today)
    checks.append(_judge("swing_candidates", "every weekday 9:35 AM", swing_d, 1, today))

    _, wu_d = _latest_dated_file("weekly_universe_*.json", today)
    checks.append(_judge("weekly_universe", "Sunday", wu_d, 6, today))

    _, disc_d = _latest_dated_file("discovery_2*.json", today)
    checks.append(_judge("discovery", "Sunday", disc_d, 6, today))

    checks.append(_universe_cache_health(today))
    checks.append(_sentiment_cache_health(today))

    stale = [c.name for c in checks if c.status == STALE]
    missing = [c.name for c in checks if c.status == MISSING]
    status = MISSING if missing else (STALE if stale else FRESH)

    return {
        "status": status,
        "stale": stale,
        "missing": missing,
        "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "last_trading_day": ltd.isoformat(),
        "artifacts": [asdict(c) for c in checks],
        "message": _message(status, stale, missing),
    }


def _message(status: str, stale: list[str], missing: list[str]) -> str:
    if status == FRESH:
        return "All pipeline data is current."
    parts = []
    if missing:
        parts.append(f"MISSING: {', '.join(missing)}")
    if stale:
        parts.append(f"STALE: {', '.join(stale)}")
    return ("Pipeline data is not current — " + "; ".join(parts)
            + ". Figures on screen may be from an earlier run.")


def scan_freshness(scan_date: Optional[str]) -> dict:
    """Freshness of ONE scan payload, for attaching to an API response so a
    consumer cannot render it as current without seeing its age."""
    today = datetime.date.today()
    try:
        d = datetime.date.fromisoformat(scan_date) if scan_date else None
    except (TypeError, ValueError):
        d = None
    h = _judge("daily_scan", "every weekday 9:35 AM", d, 1, today,
               "scan has no date")
    return {"status": h.status, "scan_date": h.last_updated,
            "age_trading_days": h.age_trading_days, "age_days": h.age_days,
            "detail": h.detail}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(pipeline_health(), indent=1))
