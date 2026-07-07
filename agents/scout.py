"""
Scout Agent — Sector funnel: macro sectors → microsectors → candidate stocks.

Scoring methodology (replaces old rank-vs-each-other approach):
  Each sector is scored against SPY on three independent signals:
    1. Relative return     — sector 60d return minus SPY 60d return
    2. Momentum accel      — sector 20d return minus sector 60d return (speeding up or slowing?)
    3. Volume breadth      — sector ETF 20d avg volume vs its own 90d avg volume

  These three signals produce a composite score (0–100).
  Sectors are then ranked LEADING / MIDDLE / LAGGING based on that score,
  not just raw 60d return vs each other.

Top-down funnel (run weekly on Sundays):
  scan_macro_sectors()   → scores all 10 macro sectors, returns top 3
  scan_microsectors()    → scores microsectors within the top 3 macro sectors
  assemble_weekly_universe() → returns candidate stock pool for the week
"""
import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

import yfinance as yf

from core.sector_map import (
    MACRO_SECTORS, MICRO_SECTORS, WILDCARD_POOL,
    get_microsectors_for_macro, get_etf_for_microsector,
)

logger = logging.getLogger(__name__)

# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SectorAssessment:
    sector: str
    etf: str
    return_20d: Optional[float]
    return_60d: Optional[float]
    return_120d: Optional[float]
    vs_spy_60d: Optional[float]       # sector 60d return minus SPY 60d return
    momentum_accel: Optional[float]   # 20d return minus 60d return (positive = accelerating)
    volume_breadth: Optional[float]   # 20d avg vol / 90d avg vol (>1.0 = rising interest)
    composite_score: float            # 0–100
    rotation_rank: Optional[str]      # LEADING / MIDDLE / LAGGING
    inflection_score: float           # kept for backward compat
    crowding_score: float
    conviction_penalty: float
    status: str
    signal: str
    notes: str


@dataclass
class WeeklyUniverse:
    date: str
    top_macro_sectors: list[str]
    top_microsectors: dict[str, list[str]]   # macro → [microsector names]
    candidate_stocks: list[str]              # deduped, ordered by microsector rank
    wildcard_stocks: list[str]
    sector_scores: dict                      # full scores for dashboard display


# ── Price / volume helpers ────────────────────────────────────────────────────

_price_cache: dict = {}


def _fetch_etf_data(etf: str) -> Optional[dict]:
    """Fetch OHLCV history for an ETF, cached per process run."""
    if etf in _price_cache:
        return _price_cache[etf]
    try:
        hist = yf.Ticker(etf).history(period="180d")
        if len(hist) < 30:
            return None
        _price_cache[etf] = hist
        return hist
    except Exception as e:
        logger.warning(f"_fetch_etf_data({etf}): {e}")
        return None


def _return_over(hist, days: int) -> Optional[float]:
    """Return over last `days` trading sessions."""
    if hist is None or len(hist) < days:
        return None
    start = float(hist["Close"].iloc[-days])
    end   = float(hist["Close"].iloc[-1])
    return (end - start) / start


def _volume_breadth(hist) -> Optional[float]:
    """20d avg volume / 90d avg volume. >1.0 means rising institutional interest."""
    if hist is None or len(hist) < 90:
        return None
    vol_20d = float(hist["Volume"].iloc[-20:].mean())
    vol_90d = float(hist["Volume"].iloc[-90:].mean())
    if vol_90d == 0:
        return None
    return round(vol_20d / vol_90d, 3)


# ── SPY baseline (cached per day) ─────────────────────────────────────────────

_spy_cache: dict = {}


def _get_spy_returns() -> dict:
    today = datetime.date.today().isoformat()
    if _spy_cache.get("date") == today:
        return _spy_cache.get("returns", {})
    hist = _fetch_etf_data("SPY")
    returns = {
        "20d":  _return_over(hist, 20)  or 0.0,
        "60d":  _return_over(hist, 60)  or 0.0,
        "120d": _return_over(hist, 120) or 0.0,
    }
    _spy_cache["date"]    = today
    _spy_cache["returns"] = returns
    return returns


# ── Composite scoring ─────────────────────────────────────────────────────────

def _composite_score(vs_spy_60d: float, momentum_accel: float, volume_breadth: float) -> float:
    """
    Combine three signals into a 0–100 score.

    Signal weights:
      Relative return vs SPY (60d)  — 50%   most important: is money flowing in?
      Momentum acceleration (20d-60d)— 30%  is it speeding up recently?
      Volume breadth (20d/90d)       — 20%  are institutions participating?
    """
    # Relative return: normalize from [-0.30, +0.30] to [0, 100]
    ret_score = max(0.0, min(100.0, (vs_spy_60d + 0.30) / 0.60 * 100))

    # Momentum accel: normalize from [-0.20, +0.20] to [0, 100]
    accel_score = max(0.0, min(100.0, (momentum_accel + 0.20) / 0.40 * 100))

    # Volume breadth: normalize from [0.5, 2.0] to [0, 100]
    breadth_score = max(0.0, min(100.0, (volume_breadth - 0.5) / 1.5 * 100))

    return round(ret_score * 0.50 + accel_score * 0.30 + breadth_score * 0.20, 1)


# ── Core assessment ───────────────────────────────────────────────────────────

def assess_sector(sector: str, etf: str) -> SectorAssessment:
    """Score a single sector/ETF against SPY on three signals."""
    spy = _get_spy_returns()
    hist = _fetch_etf_data(etf)

    ret_20d  = _return_over(hist, 20)  or 0.0
    ret_60d  = _return_over(hist, 60)  or 0.0
    ret_120d = _return_over(hist, 120) or 0.0

    vs_spy_60d     = ret_60d - spy["60d"]
    momentum_accel = ret_20d - ret_60d
    vol_breadth    = _volume_breadth(hist) or 1.0

    score = _composite_score(vs_spy_60d, momentum_accel, vol_breadth)

    # Crowding: sector running very hot short-term
    crowding_score = 0.0
    if ret_20d > 0.20:
        crowding_score = 80.0
    elif ret_20d > 0.10:
        crowding_score = 40.0

    # Inflection: stacking momentum across timeframes
    inflection_score = 50.0
    if ret_120d > 0.15: inflection_score += 20
    if ret_60d  > 0.10: inflection_score += 15
    if ret_20d  > 0.05: inflection_score += 15
    inflection_score = min(100.0, inflection_score)

    # Status and conviction penalty
    conviction_penalty = 0.0
    notes_parts = []

    if ret_60d <= -0.25:
        status = "MAJOR_HEADWIND"
        signal = "WAIT"
        conviction_penalty = -2.0
        notes_parts.append(f"Down {ret_60d:.0%} in 60d vs SPY {spy['60d']:+.0%}")
    elif ret_60d <= -0.15:
        status = "HEADWIND"
        signal = "WATCHLIST"
        conviction_penalty = -1.0
        notes_parts.append(f"Down {ret_60d:.0%} in 60d — fighting the tide")
    elif ret_20d >= 0.30:
        status = "BUBBLE"
        signal = "WATCHLIST"
        conviction_penalty = -1.0
        notes_parts.append(f"Up {ret_20d:.0%} in 20d — overextended")
    elif vs_spy_60d >= 0.10:
        status = "TAILWIND"
        signal = "BUY_OK"
        conviction_penalty = +0.5
        notes_parts.append(f"Outperforming SPY by {vs_spy_60d:+.0%} over 60d")
    elif vs_spy_60d >= 0.0:
        status = "NEUTRAL_POSITIVE"
        signal = "BUY_OK"
        conviction_penalty = +0.2
        notes_parts.append(f"Slightly ahead of SPY ({vs_spy_60d:+.1%} over 60d)")
    else:
        status = "NEUTRAL"
        signal = "BUY_OK"
        conviction_penalty = 0.0
        notes_parts.append(f"Lagging SPY by {vs_spy_60d:.1%} over 60d")

    if momentum_accel > 0.05:
        notes_parts.append(f"Accelerating (+{momentum_accel:.1%} 20d vs 60d)")
    elif momentum_accel < -0.05:
        notes_parts.append(f"Decelerating ({momentum_accel:.1%} 20d vs 60d)")
        conviction_penalty -= 0.2

    if vol_breadth > 1.3:
        notes_parts.append(f"Volume surge ({vol_breadth:.1f}x 90d avg)")
    elif vol_breadth < 0.8:
        notes_parts.append(f"Low volume ({vol_breadth:.1f}x 90d avg) — weak conviction")

    return SectorAssessment(
        sector=sector,
        etf=etf,
        return_20d=round(ret_20d, 4),
        return_60d=round(ret_60d, 4),
        return_120d=round(ret_120d, 4),
        vs_spy_60d=round(vs_spy_60d, 4),
        momentum_accel=round(momentum_accel, 4),
        volume_breadth=round(vol_breadth, 3),
        composite_score=score,
        rotation_rank=None,  # set after all sectors scored
        inflection_score=round(inflection_score, 1),
        crowding_score=round(crowding_score, 1),
        conviction_penalty=round(conviction_penalty, 2),
        status=status,
        signal=signal,
        notes=" | ".join(notes_parts),
    )


def _assign_ranks(assessments: dict[str, SectorAssessment]) -> dict[str, SectorAssessment]:
    """Assign LEADING/MIDDLE/LAGGING based on composite score, not raw return rank."""
    if not assessments:
        return assessments
    scored = sorted(assessments.items(), key=lambda x: -x[1].composite_score)
    n = len(scored)
    top    = max(1, n // 3)
    bottom = max(1, n // 3)
    for i, (name, s) in enumerate(scored):
        if i < top:
            s.rotation_rank = "LEADING"
        elif i >= n - bottom:
            s.rotation_rank = "LAGGING"
        else:
            s.rotation_rank = "MIDDLE"
    return assessments


# ── Public scan functions ─────────────────────────────────────────────────────

def scan_macro_sectors() -> dict[str, SectorAssessment]:
    """Score all 10 macro sectors. Used by weekly scan and weekly review."""
    results = {}
    for sector, data in MACRO_SECTORS.items():
        etf = data["etf"]
        logger.info(f"Scoring macro sector: {sector} ({etf})")
        results[sector] = assess_sector(sector, etf)
    return _assign_ranks(results)


def scan_microsectors(macro_sectors: list[str]) -> dict[str, SectorAssessment]:
    """
    Score all microsectors within the given macro sectors.
    Called with the top 3 leading macro sectors from scan_macro_sectors().
    """
    results = {}
    for macro in macro_sectors:
        for ms_name, ms_data in get_microsectors_for_macro(macro).items():
            etf = get_etf_for_microsector(ms_name)
            if not etf:
                logger.warning(f"No ETF proxy for microsector {ms_name} — skipping")
                continue
            logger.info(f"Scoring microsector: {ms_name} ({etf})")
            results[ms_name] = assess_sector(ms_name, etf)
    return _assign_ranks(results)


def assemble_weekly_universe(
    top_macro: list[str],
    top_micro: dict[str, list[str]],
    micro_assessments: dict[str, SectorAssessment],
) -> list[str]:
    """
    Build the week's candidate stock pool from the top microsectors.
    Stocks are ordered: top microsector stocks first, then lower microsectors.
    """
    seen = set()
    ordered = []

    # For each macro sector, take its top microsectors ordered by score
    for macro in top_macro:
        micro_names = top_micro.get(macro, [])
        # Sort microsectors by composite score descending
        micro_names_sorted = sorted(
            micro_names,
            key=lambda m: micro_assessments.get(m, SectorAssessment(
                m, "", 0, 0, 0, 0, 0, 1, 0, None, 50, 0, 0, "NEUTRAL", "BUY_OK", ""
            )).composite_score,
            reverse=True,
        )
        for ms in micro_names_sorted:
            stocks = MICRO_SECTORS.get(ms, {}).get("stocks", [])
            for ticker in stocks:
                if ticker not in seen:
                    seen.add(ticker)
                    ordered.append(ticker)

    return ordered


def run_weekly_funnel(top_n_macro: int = 3, top_n_micro: int = 3) -> WeeklyUniverse:
    """
    Full top-down funnel. Called by the Sunday weekly scan workflow.

    Returns WeeklyUniverse with:
      - top macro sectors
      - top microsectors per macro
      - ordered candidate stock pool
      - wildcard stocks
      - full sector scores for dashboard display
    """
    today = datetime.date.today().isoformat()
    logger.info("=== Weekly sector funnel starting ===")

    # Step 1: Score all macro sectors
    logger.info("Step 1: Scoring macro sectors...")
    macro_scores = scan_macro_sectors()
    top_macro = [
        name for name, _ in
        sorted(macro_scores.items(), key=lambda x: -x[1].composite_score)[:top_n_macro]
    ]
    logger.info(f"Top {top_n_macro} macro sectors: {top_macro}")

    # Step 2: Score microsectors within top macro sectors
    logger.info("Step 2: Scoring microsectors within top macro sectors...")
    micro_scores = scan_microsectors(top_macro)

    # Pick top N microsectors per macro sector
    top_micro: dict[str, list[str]] = {}
    for macro in top_macro:
        candidates = {
            name: s for name, s in micro_scores.items()
            if MICRO_SECTORS.get(name, {}).get("macro") == macro
        }
        top_micro[macro] = [
            name for name, _ in
            sorted(candidates.items(), key=lambda x: -x[1].composite_score)[:top_n_micro]
        ]
        logger.info(f"  {macro} → top microsectors: {top_micro[macro]}")

    # Step 3: Assemble candidate stock pool
    logger.info("Step 3: Assembling candidate stock pool...")
    candidates = assemble_weekly_universe(top_macro, top_micro, micro_scores)
    logger.info(f"Candidate pool: {len(candidates)} stocks + {len(WILDCARD_POOL)} wildcards")

    # Build score dict for dashboard
    sector_scores = {
        "macro": {
            name: {
                "etf": s.etf,
                "return_20d": s.return_20d,
                "return_60d": s.return_60d,
                "vs_spy_60d": s.vs_spy_60d,
                "momentum_accel": s.momentum_accel,
                "volume_breadth": s.volume_breadth,
                "composite_score": s.composite_score,
                "rotation_rank": s.rotation_rank,
                "status": s.status,
                "notes": s.notes,
            }
            for name, s in macro_scores.items()
        },
        "micro": {
            name: {
                "etf": s.etf,
                "macro": MICRO_SECTORS.get(name, {}).get("macro"),
                "return_20d": s.return_20d,
                "return_60d": s.return_60d,
                "vs_spy_60d": s.vs_spy_60d,
                "momentum_accel": s.momentum_accel,
                "volume_breadth": s.volume_breadth,
                "composite_score": s.composite_score,
                "rotation_rank": s.rotation_rank,
                "status": s.status,
                "notes": s.notes,
            }
            for name, s in micro_scores.items()
        },
    }

    return WeeklyUniverse(
        date=today,
        top_macro_sectors=top_macro,
        top_microsectors=top_micro,
        candidate_stocks=candidates,
        wildcard_stocks=WILDCARD_POOL,
        sector_scores=sector_scores,
    )


# ── Backward-compatible wrapper ───────────────────────────────────────────────
# weekly_review.py and other callers use scan_all_sectors() — keep it working.

def scan_all_sectors() -> dict[str, SectorAssessment]:
    """Backward-compatible: score all macro sectors (used by weekly review)."""
    return scan_macro_sectors()
