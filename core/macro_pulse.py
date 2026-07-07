"""
Macro Overlay — fetches macro indicators (10Y yield, DXY, credit spreads)
and computes a conviction penalty when macro headwinds are active.

Cached daily to: data/macro_pulse_{date}.json
"""
import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_DIR = "data"

# Fed FOMC meeting dates 2025-2026 (hardcoded)
FED_MEETING_DATES = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]


@dataclass
class MacroPulse:
    ten_year_yield: Optional[float]     # current 10Y Treasury yield
    yield_trend: str                    # "RISING" | "FALLING" | "FLAT"
    dxy_trend: str                      # "RISING" | "FALLING" | "FLAT" — dollar index
    credit_spread_signal: str           # "WIDE" | "NORMAL" | "TIGHT" — risk appetite
    fed_meeting_within_days: Optional[int]  # days to next Fed meeting (None if >30d away)
    headwinds: list[str]                # human-readable list of active headwinds
    headwind_count: int
    conviction_penalty: float           # 0.0 to 1.5 subtracted from conviction
    note: str


def _trend(short_avg: float, long_avg: float, threshold: float = 0.02) -> str:
    """Compare short vs long moving average to determine trend direction."""
    pct_diff = (short_avg - long_avg) / long_avg if long_avg != 0 else 0
    if pct_diff > threshold:
        return "RISING"
    elif pct_diff < -threshold:
        return "FALLING"
    return "FLAT"


def _days_to_next_fed() -> Optional[int]:
    today = datetime.date.today()
    for date_str in FED_MEETING_DATES:
        meeting_date = datetime.date.fromisoformat(date_str)
        if meeting_date >= today:
            return (meeting_date - today).days
    return None


def get_macro_pulse() -> MacroPulse:
    """
    Fetch macro indicators and compute conviction penalty.
    Results cached per day.
    """
    today = datetime.date.today().isoformat()
    cache_path = os.path.join(CACHE_DIR, f"macro_pulse_{today}.json")

    # Load from cache if available
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                d = json.load(f)
            pulse = MacroPulse(**d)
            logger.debug(f"[MacroPulse] Loaded from cache: {cache_path}")
            return pulse
        except Exception as e:
            logger.debug(f"[MacroPulse] Cache load failed: {e}")

    # Fetch fresh data
    try:
        import yfinance as yf
    except ImportError:
        return _fallback_pulse("yfinance not available")

    ten_year_yield = None
    yield_trend = "FLAT"
    dxy_trend = "FLAT"
    credit_spread_signal = "NORMAL"

    # 10Y Treasury yield (^TNX)
    try:
        tnx = yf.download("^TNX", period="30d", interval="1d", progress=False, auto_adjust=True)
        if not tnx.empty and len(tnx) >= 10:
            if hasattr(tnx.columns, "get_level_values"):
                tnx.columns = tnx.columns.get_level_values(0)
            closes = tnx["Close"].dropna().values
            ten_year_yield = round(float(closes[-1]), 3)
            short_avg = float(closes[-5:].mean())
            long_avg = float(closes[-20:].mean())
            yield_trend = _trend(short_avg, long_avg, threshold=0.01)
    except Exception as e:
        logger.debug(f"[MacroPulse] TNX fetch failed: {e}")

    # DXY (DX-Y.NYB)
    try:
        dxy = yf.download("DX-Y.NYB", period="30d", interval="1d", progress=False, auto_adjust=True)
        if not dxy.empty and len(dxy) >= 10:
            if hasattr(dxy.columns, "get_level_values"):
                dxy.columns = dxy.columns.get_level_values(0)
            closes = dxy["Close"].dropna().values
            short_avg = float(closes[-5:].mean())
            long_avg = float(closes[-20:].mean())
            dxy_trend = _trend(short_avg, long_avg, threshold=0.005)
    except Exception as e:
        logger.debug(f"[MacroPulse] DXY fetch failed: {e}")

    # Credit spread proxy: HYG/LQD ratio
    try:
        import numpy as np
        hyg = yf.download("HYG", period="35d", interval="1d", progress=False, auto_adjust=True)
        lqd = yf.download("LQD", period="35d", interval="1d", progress=False, auto_adjust=True)
        if not hyg.empty and not lqd.empty:
            if hasattr(hyg.columns, "get_level_values"):
                hyg.columns = hyg.columns.get_level_values(0)
            if hasattr(lqd.columns, "get_level_values"):
                lqd.columns = lqd.columns.get_level_values(0)
            hyg_close = hyg["Close"].dropna()
            lqd_close = lqd["Close"].dropna()
            # Align by date
            combined = hyg_close.rename("HYG").to_frame().join(lqd_close.rename("LQD"), how="inner")
            if len(combined) >= 30:
                ratio = combined["HYG"] / combined["LQD"]
                current_ratio = float(ratio.iloc[-1])
                avg_30d = float(ratio.mean())
                if current_ratio < avg_30d * 0.98:
                    credit_spread_signal = "WIDE"  # HYG falling relative to LQD = spreads widening
                elif current_ratio > avg_30d * 1.02:
                    credit_spread_signal = "TIGHT"
                else:
                    credit_spread_signal = "NORMAL"
    except Exception as e:
        logger.debug(f"[MacroPulse] Credit spread fetch failed: {e}")

    # Fed meeting proximity
    fed_days = _days_to_next_fed()
    fed_within_days = fed_days if fed_days is not None and fed_days <= 30 else None

    # Build headwinds list
    headwinds = []
    if yield_trend == "RISING":
        headwinds.append("10Y yield rising — headwind for growth stocks (higher discount rate)")
    if dxy_trend == "RISING":
        headwinds.append("DXY rising — headwind for multinational earnings")
    if credit_spread_signal == "WIDE":
        headwinds.append("Credit spreads widening — risk-off environment")
    if fed_within_days is not None and fed_within_days <= 7:
        headwinds.append(f"Fed meeting in {fed_within_days} days — uncertainty/volatility risk")

    headwind_count = len(headwinds)

    # Conviction penalty
    if headwind_count == 0 or headwind_count == 1:
        conviction_penalty = 0.0
    elif headwind_count == 2:
        conviction_penalty = 0.5
    elif headwind_count == 3:
        conviction_penalty = 1.0
    else:
        conviction_penalty = 1.5

    note = (
        f"10Y={ten_year_yield or 'N/A'}% ({yield_trend}) | "
        f"DXY={dxy_trend} | Credit={credit_spread_signal} | "
        f"Fed={'in ' + str(fed_within_days) + 'd' if fed_within_days else '>30d'} | "
        f"Headwinds={headwind_count} → penalty={conviction_penalty:.1f}"
    )

    pulse = MacroPulse(
        ten_year_yield=ten_year_yield,
        yield_trend=yield_trend,
        dxy_trend=dxy_trend,
        credit_spread_signal=credit_spread_signal,
        fed_meeting_within_days=fed_within_days,
        headwinds=headwinds,
        headwind_count=headwind_count,
        conviction_penalty=conviction_penalty,
        note=note,
    )

    # Cache
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(asdict(pulse), f, indent=2)
    except Exception as e:
        logger.debug(f"[MacroPulse] Cache save failed: {e}")

    logger.info(f"[MacroPulse] {note}")
    return pulse


def _fallback_pulse(reason: str) -> MacroPulse:
    return MacroPulse(
        ten_year_yield=None,
        yield_trend="FLAT",
        dxy_trend="FLAT",
        credit_spread_signal="NORMAL",
        fed_meeting_within_days=None,
        headwinds=[],
        headwind_count=0,
        conviction_penalty=0.0,
        note=f"Macro data unavailable: {reason}",
    )
