"""
Vision Cache — stores chart vision results and decides when to re-scan.

Key insight: chart patterns don't change daily. The cached vision result
stays valid until a meaningful price event (breakout, breakdown, volume spike,
MA crossover) invalidates it. This reduces vision API calls by ~70-80%.

Cache invalidation triggers (checked numerically, no API cost):
  1. Price breaks above cached resistance  → pattern may have completed (breakout)
  2. Price breaks below cached support     → pattern may have failed (breakdown)
  3. Volume spike > 2x 20-day average     → something happening, re-assess
  4. New MA crossover detected             → structure has changed
  5. Cache age > max_age_days             → force refresh regardless

Cache location: data/vision_cache/{ticker}_{timeframe}.json
"""
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import yfinance as yf

from agents.chart_vision import ChartVisionResult

logger = logging.getLogger(__name__)

CACHE_DIR = "data/vision_cache"


@dataclass
class VisionCacheEntry:
    ticker: str
    timeframe: str           # "weekly" or "daily"
    scanned_at: str          # ISO date string
    max_age_days: int        # 7 for weekly, 3 for daily
    # Vision result fields
    pattern: str
    pattern_confidence: float
    ma_crossover_type: str
    ma_crossover_recency: str
    ma_crossover_quality: str
    price_structure: str
    support_levels: list
    resistance_levels: list
    pattern_thesis: str
    actionable_lean: str
    chart_score_delta: float
    # Invalidation thresholds derived from vision result
    invalidate_above: Optional[float]   # lowest resistance — break above = re-scan
    invalidate_below: Optional[float]   # highest support — break below = re-scan
    price_at_scan: float                # price when scan was run


def _cache_path(ticker: str, timeframe: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{ticker}_{timeframe}.json")


def save(result: ChartVisionResult, timeframe: str, price_at_scan: float) -> None:
    """Persist a vision result to cache with invalidation thresholds."""
    max_age = 7 if timeframe == "weekly" else 3

    # Set invalidation thresholds from the vision result's S/R levels
    invalidate_above = min(result.resistance_levels) if result.resistance_levels else None
    invalidate_below = max(result.support_levels) if result.support_levels else None

    entry = VisionCacheEntry(
        ticker=result.ticker,
        timeframe=timeframe,
        scanned_at=date.today().isoformat(),
        max_age_days=max_age,
        pattern=result.pattern,
        pattern_confidence=result.pattern_confidence,
        ma_crossover_type=result.ma_crossover_type,
        ma_crossover_recency=result.ma_crossover_recency,
        ma_crossover_quality=result.ma_crossover_quality,
        price_structure=result.price_structure,
        support_levels=result.support_levels,
        resistance_levels=result.resistance_levels,
        pattern_thesis=result.pattern_thesis,
        actionable_lean=result.actionable_lean,
        chart_score_delta=result.chart_score_delta,
        invalidate_above=invalidate_above,
        invalidate_below=invalidate_below,
        price_at_scan=price_at_scan,
    )

    path = _cache_path(result.ticker, timeframe)
    with open(path, "w") as f:
        json.dump(asdict(entry), f, indent=2)
    logger.debug(f"[VisionCache] Saved {result.ticker}/{timeframe} → {path}")


def load(ticker: str, timeframe: str) -> Optional[VisionCacheEntry]:
    """Load cached entry if it exists. Returns None if no cache file."""
    path = _cache_path(ticker, timeframe)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return VisionCacheEntry(**data)
    except Exception as e:
        logger.warning(f"[VisionCache] Failed to load cache for {ticker}/{timeframe}: {e}")
        return None


def _fetch_current_price_and_signals(ticker: str) -> Optional[dict]:
    """Fetch latest price, volume, and MA data for invalidation checks."""
    try:
        raw = yf.download(ticker, period="60d", interval="1d", progress=False, auto_adjust=True)
        if raw is None or len(raw) < 20:
            return None

        if hasattr(raw.columns, 'get_level_values'):
            raw.columns = raw.columns.get_level_values(0)

        close = raw["Close"]
        volume = raw["Volume"]
        current_price = float(close.iloc[-1])
        avg_volume_20d = float(volume.iloc[-21:-1].mean())
        current_volume = float(volume.iloc[-1])

        ma50 = close.rolling(50).mean()
        ma200 = close.rolling(200).mean()

        # Detect new MA crossover in last 5 sessions
        cross = (ma50 > ma200).astype(int)
        cross_signal = cross.diff().iloc[-5:]
        new_crossover = (cross_signal != 0).any()

        return {
            "current_price": current_price,
            "current_volume": current_volume,
            "avg_volume_20d": avg_volume_20d,
            "new_crossover": bool(new_crossover),
        }
    except Exception as e:
        logger.warning(f"[VisionCache] Could not fetch price signals for {ticker}: {e}")
        return None


def needs_rescan(ticker: str, timeframe: str) -> tuple[bool, str]:
    """
    Check whether a cached vision result is still valid.
    Returns (should_rescan, reason).
    """
    entry = load(ticker, timeframe)

    if entry is None:
        return True, "no_cache"

    # 1. Age check
    scanned = datetime.fromisoformat(entry.scanned_at).date()
    age_days = (date.today() - scanned).days
    if age_days >= entry.max_age_days:
        return True, f"cache_expired_{age_days}d"

    # 2. Fetch current market data for trigger checks
    signals = _fetch_current_price_and_signals(ticker)
    if signals is None:
        return False, "fetch_failed_use_cache"

    price = signals["current_price"]
    volume = signals["current_volume"]
    avg_vol = signals["avg_volume_20d"]

    # 3. Price broke above resistance (potential breakout)
    if entry.invalidate_above and price > entry.invalidate_above * 1.005:
        return True, f"price_broke_resistance_{entry.invalidate_above:.2f}"

    # 4. Price broke below support (potential breakdown)
    if entry.invalidate_below and price < entry.invalidate_below * 0.995:
        return True, f"price_broke_support_{entry.invalidate_below:.2f}"

    # 5. Volume spike (>2x average — something is happening)
    if avg_vol > 0 and volume > avg_vol * 2.0:
        return True, f"volume_spike_{volume/avg_vol:.1f}x"

    # 6. New MA crossover detected
    if signals["new_crossover"]:
        return True, "new_ma_crossover"

    return False, f"cache_valid_{age_days}d_old"


def get_or_fetch(
    ticker: str,
    timeframe: str,
    period: str,
    interval: str,
    current_price: float,
) -> ChartVisionResult:
    """
    Main entry point for the orchestrator.
    Returns cached result if valid, otherwise runs vision and caches result.
    """
    from agents.chart_vision import run as vision_run

    rescan, reason = needs_rescan(ticker, timeframe)

    if not rescan:
        entry = load(ticker, timeframe)
        logger.info(f"[VisionCache] {ticker}/{timeframe} cache hit — {reason}")
        return _entry_to_result(entry)

    logger.info(f"[VisionCache] {ticker}/{timeframe} running vision — {reason}")
    result = vision_run(ticker, period=period, interval=interval)

    if result.error is None:
        save(result, timeframe, current_price)

    return result


def _entry_to_result(entry: VisionCacheEntry) -> ChartVisionResult:
    """Reconstruct a ChartVisionResult from a cache entry."""
    return ChartVisionResult(
        ticker=entry.ticker,
        pattern=entry.pattern,
        pattern_confidence=entry.pattern_confidence,
        ma_crossover_type=entry.ma_crossover_type,
        ma_crossover_recency=entry.ma_crossover_recency,
        ma_crossover_quality=entry.ma_crossover_quality,
        price_structure=entry.price_structure,
        support_levels=entry.support_levels,
        resistance_levels=entry.resistance_levels,
        pattern_thesis=entry.pattern_thesis,
        actionable_lean=entry.actionable_lean,
        chart_score_delta=entry.chart_score_delta,
    )


def invalidate(ticker: str, timeframe: str = None) -> None:
    """Force-invalidate cache for a ticker (both timeframes if timeframe=None)."""
    timeframes = [timeframe] if timeframe else ["weekly", "daily"]
    for tf in timeframes:
        path = _cache_path(ticker, tf)
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"[VisionCache] Invalidated {ticker}/{tf}")
