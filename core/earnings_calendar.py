"""
Earnings Calendar Integration [GAP 10]

Four-phase earnings gate:
  NORMAL        — >14 days out: full scoring, normal stops
  EARNINGS_WATCH — 7-14 days: flag on dashboard, no new buys above conviction 8
  EARNINGS_CAUTION — 2-7 days: WATCHLIST only, widen stops by 1 ATR
  EARNINGS_BLACKOUT — <48h: no new positions; existing stops widened by 1.5 ATR

Uses yfinance calendar (free, no API key required).
"""
import datetime
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)


class EarningsPhase(str, Enum):
    NORMAL = "NORMAL"
    EARNINGS_WATCH = "EARNINGS_WATCH"       # 7-14 days out
    EARNINGS_CAUTION = "EARNINGS_CAUTION"   # 2-7 days out — WATCHLIST only
    EARNINGS_BLACKOUT = "EARNINGS_BLACKOUT" # <48h — no new entries


@dataclass
class EarningsStatus:
    ticker: str
    phase: EarningsPhase
    next_earnings_date: Optional[datetime.date]
    days_to_earnings: Optional[int]

    # Actions to apply
    block_new_buy: bool           # True in CAUTION + BLACKOUT
    widen_stops_atr_mult: float   # extra ATR multiplier added to all stop tiers
    conviction_cap: Optional[float]  # None = no cap; float = hard ceiling

    alert: Optional[str]


def get_next_earnings(ticker: str) -> Optional[datetime.date]:
    """Fetch next earnings date from yfinance. Returns None if unavailable."""
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None:
            return None

        # yfinance returns calendar as dict (current) or DataFrame (older versions)
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date") or []
            raw = dates[0] if dates else None
        elif hasattr(cal, "empty"):
            if cal.empty:
                return None
            if "Earnings Date" in cal.columns:
                raw = cal["Earnings Date"].iloc[0]
            else:
                raw = None
        else:
            raw = None

        if raw is None:
            return None

        if isinstance(raw, datetime.datetime):
            return raw.date()
        if isinstance(raw, datetime.date):
            return raw
        if isinstance(raw, str):
            return datetime.date.fromisoformat(raw[:10])
        return None

    except Exception as e:
        logger.debug(f"{ticker}: earnings calendar unavailable ({e})")
        return None


def check_earnings_gate(
    ticker: str,
    earnings_date: Optional[datetime.date] = None,
    now: Optional[datetime.datetime] = None,
) -> EarningsStatus:
    """
    Returns earnings gate status for ticker.
    Pass earnings_date to skip the yfinance fetch (useful in batch runs).
    """
    now = now or datetime.datetime.now()
    today = now.date()

    if earnings_date is None:
        earnings_date = get_next_earnings(ticker)

    if earnings_date is None:
        return EarningsStatus(
            ticker=ticker,
            phase=EarningsPhase.NORMAL,
            next_earnings_date=None,
            days_to_earnings=None,
            block_new_buy=False,
            widen_stops_atr_mult=0.0,
            conviction_cap=None,
            alert=None,
        )

    days_out = (earnings_date - today).days

    if days_out < 0:
        # Earnings already passed
        return EarningsStatus(
            ticker=ticker,
            phase=EarningsPhase.NORMAL,
            next_earnings_date=earnings_date,
            days_to_earnings=days_out,
            block_new_buy=False,
            widen_stops_atr_mult=0.0,
            conviction_cap=None,
            alert=None,
        )

    if days_out <= 2:
        return EarningsStatus(
            ticker=ticker,
            phase=EarningsPhase.EARNINGS_BLACKOUT,
            next_earnings_date=earnings_date,
            days_to_earnings=days_out,
            block_new_buy=True,
            widen_stops_atr_mult=1.5,
            conviction_cap=0.0,
            alert=(
                f"{ticker}: EARNINGS BLACKOUT — {days_out}d to earnings on {earnings_date}. "
                "No new entries. Existing stops widened by 1.5× ATR."
            ),
        )

    if days_out <= 7:
        return EarningsStatus(
            ticker=ticker,
            phase=EarningsPhase.EARNINGS_CAUTION,
            next_earnings_date=earnings_date,
            days_to_earnings=days_out,
            block_new_buy=True,
            widen_stops_atr_mult=1.0,
            conviction_cap=7.9,  # forces WATCHLIST even if conviction=8+
            alert=(
                f"{ticker}: EARNINGS CAUTION — {days_out}d to earnings on {earnings_date}. "
                "No new buys. Stops widened by 1× ATR."
            ),
        )

    if days_out <= 14:
        return EarningsStatus(
            ticker=ticker,
            phase=EarningsPhase.EARNINGS_WATCH,
            next_earnings_date=earnings_date,
            days_to_earnings=days_out,
            block_new_buy=False,
            widen_stops_atr_mult=0.0,
            conviction_cap=None,
            alert=(
                f"{ticker}: EARNINGS WATCH — {days_out}d to earnings on {earnings_date}. "
                "Monitor closely. No action required yet."
            ),
        )

    return EarningsStatus(
        ticker=ticker,
        phase=EarningsPhase.NORMAL,
        next_earnings_date=earnings_date,
        days_to_earnings=days_out,
        block_new_buy=False,
        widen_stops_atr_mult=0.0,
        conviction_cap=None,
        alert=None,
    )


def apply_earnings_stop_widening(
    stops,  # StopLevels — avoid circular import
    earnings: EarningsStatus,
    atr: float,
) -> object:
    """
    Widens stop tiers by earnings.widen_stops_atr_mult * ATR.
    Returns the original stops unchanged if no widening needed.
    """
    if earnings.widen_stops_atr_mult == 0.0:
        return stops

    extra = atr * earnings.widen_stops_atr_mult
    from core.stop_loss import StopLevels
    return StopLevels(
        ticker=stops.ticker,
        entry_price=stops.entry_price,
        current_price=stops.current_price,
        atr=stops.atr,
        conviction=stops.conviction,
        tier1=round(stops.tier1 - extra, 2),
        tier2=round(stops.tier2 - extra, 2),
        tier3=round(stops.tier3 - extra, 2),
        phase=stops.phase,
        gain_pct=stops.gain_pct,
        trailing_stop=round(stops.trailing_stop - extra, 2) if stops.trailing_stop else None,
        notes=f"{stops.notes} | EARNINGS ({earnings.phase.value}) — stops widened by {extra:.2f}",
    )
