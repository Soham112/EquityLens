"""
Three-Tier ATR Stop Loss System (Part 9).
Stops only move UP, never down.
Phase progression: Entry → Breakeven → Trailing → Peak Protection.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class StopPhase(str, Enum):
    PHASE_1 = "1 — Initial (ATR floor)"
    PHASE_2 = "2 — Breakeven"
    PHASE_3 = "3 — Trailing 10%"
    PHASE_4 = "4 — Peak Protection 6-8%"


@dataclass
class StopLevels:
    ticker: str
    entry_price: float
    current_price: float
    atr: float
    conviction: int

    tier1: float    # Alert — don't auto-sell
    tier2: float    # Confirmation — re-evaluate
    tier3: float    # Hard stop — exit

    phase: StopPhase
    gain_pct: float
    trailing_stop: Optional[float]   # active trailing stop price if in phase 3/4
    notes: str

    @property
    def status(self) -> str:
        if self.current_price <= self.tier3:
            return "TIER3_HIT"
        if self.current_price <= self.tier1:
            return "TIER1_ALERT"
        return "NORMAL"


def _atr_multipliers(conviction: int) -> tuple[float, float, float]:
    """Returns (tier1_mult, tier2_mult, tier3_mult) based on conviction."""
    if conviction >= 8:
        return (2.5, 3.5, 4.5)
    return (2.0, 3.0, 4.0)


def calculate_stops(
    ticker: str,
    entry_price: float,
    current_price: float,
    atr: float,
    conviction: int,
    peak_price: Optional[float] = None,
) -> StopLevels:
    gain_pct = (current_price - entry_price) / entry_price
    peak = peak_price or current_price

    t1_mult, t2_mult, t3_mult = _atr_multipliers(conviction)

    # Base stops (from entry)
    tier1_base = entry_price - (t1_mult * atr)
    tier2_base = entry_price - (t2_mult * atr)
    tier3_base = entry_price - (t3_mult * atr)

    trailing_stop = None

    if gain_pct < 0.15:
        # Phase 1: initial ATR floor
        phase = StopPhase.PHASE_1
        tier1 = tier1_base
        tier2 = tier2_base
        tier3 = tier3_base
        notes = "Initial entry: ATR-based hard floor"

    elif gain_pct < 0.25:
        # Phase 2: move stops to breakeven
        phase = StopPhase.PHASE_2
        tier1 = max(tier1_base, entry_price)   # never below breakeven
        tier2 = max(tier2_base, entry_price * 0.98)
        tier3 = max(tier3_base, entry_price * 0.96)
        notes = "Breakeven: thesis confirmed, downside eliminated"

    elif gain_pct < 0.50:
        # Phase 3: trailing stop 10% below peak
        phase = StopPhase.PHASE_3
        trailing_stop = peak * 0.90
        tier1 = max(tier1_base, trailing_stop)
        tier2 = max(tier2_base, trailing_stop * 0.98)
        tier3 = max(tier3_base, trailing_stop * 0.96)
        notes = "Trailing 10% — stop moves up with price, never down"

    else:
        # Phase 4: peak protection 6-8%
        phase = StopPhase.PHASE_4
        trailing_stop = peak * 0.92
        tier1 = max(tier1_base, trailing_stop)
        tier2 = max(tier2_base, trailing_stop * 0.98)
        tier3 = max(tier3_base, trailing_stop * 0.96)
        notes = "Peak protection: 8% trail, protecting mega-winner gains"

    return StopLevels(
        ticker=ticker,
        entry_price=entry_price,
        current_price=current_price,
        atr=atr,
        conviction=conviction,
        tier1=round(tier1, 2),
        tier2=round(tier2, 2),
        tier3=round(tier3, 2),
        phase=phase,
        gain_pct=gain_pct,
        trailing_stop=round(trailing_stop, 2) if trailing_stop else None,
        notes=notes,
    )


def apply_vix_spike_widening(stops: StopLevels, vix_level: float) -> StopLevels:
    """
    GAP 7: When VIX > 30, widen all stop tiers by 50% of ATR distance.
    This prevents getting shaken out by volatility spikes on good positions.
    Stops still cannot move down — returns same object if VIX is normal.
    """
    if vix_level <= 30:
        return stops

    extra = stops.atr * 0.5  # widen by half an ATR
    widened = StopLevels(
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
        notes=f"{stops.notes} | VIX SPIKE ({vix_level:.0f}) — stops widened by {extra:.2f}",
    )
    return widened


def reevaluate_stop(ticker: str, current_stop: float, entry_price: float) -> dict:
    """
    Re-examine S/R levels. If a new support has formed ABOVE current_stop,
    return the new stop = new_S1 - 0.5*ATR.
    Called weekly on all held positions.
    Returns: {ticker, old_stop, new_stop, updated: bool, reason: str}
    """
    result = {
        "ticker": ticker,
        "old_stop": current_stop,
        "new_stop": current_stop,
        "updated": False,
        "reason": "No change",
    }

    try:
        from core.swing_chart_analysis import _fetch_ohlcv, _find_sr_levels, _calc_atr

        df = _fetch_ohlcv(ticker)
        if df is None or df.empty:
            result["reason"] = "Could not fetch OHLCV data"
            return result

        current_price = float(df["Close"].iloc[-1])
        atr = _calc_atr(df)
        sr_levels = _find_sr_levels(df, current_price)

        # Find support levels above the current stop but below entry price
        supports_above_stop = [
            lvl for lvl in sr_levels
            if lvl.kind == "support"
            and lvl.price > current_stop
            and lvl.price < entry_price
        ]

        if not supports_above_stop:
            result["reason"] = "No new support levels found above current stop"
            return result

        # Use the highest support level (closest to current price)
        best_support = max(supports_above_stop, key=lambda x: x.price)
        new_stop = round(best_support.price - 0.5 * atr, 2)

        # Only raise the stop, never lower it
        if new_stop <= current_stop:
            result["reason"] = f"New support at {best_support.price:.2f} but new stop {new_stop:.2f} not higher than current {current_stop:.2f}"
            return result

        # Don't move stop above entry price
        if new_stop >= entry_price:
            result["reason"] = f"New stop {new_stop:.2f} would exceed entry price {entry_price:.2f} — skipped"
            return result

        result["new_stop"] = new_stop
        result["updated"] = True
        result["reason"] = (
            f"New support formed at {best_support.price:.2f} ({best_support.label}, "
            f"{best_support.tests} tests) — stop raised {current_stop:.2f} → {new_stop:.2f} "
            f"(S1 - 0.5×ATR={atr:.2f})"
        )
        logger.info(f"[StopLoss] {ticker}: stop raised {current_stop:.2f} → {new_stop:.2f}")

    except Exception as e:
        result["reason"] = f"Error during stop re-evaluation: {e}"
        logger.debug(f"[StopLoss] {ticker} reevaluate_stop error: {e}")

    return result


def check_trimming_levels(
    entry_price: float,
    current_price: float,
    position_value: float,
    already_taken: Optional[list[str]] = None,
) -> Optional[dict]:
    """
    Profit-taking tiers — raised thresholds so strong growth stocks aren't
    trimmed prematurely. First trim only at +100%, not +50%.

    already_taken: trim actions this position has already executed
    (e.g. ["TRIM_25"]). Each level fires exactly once — without this the
    daily monitor re-trimmed 25% every single day the gain stayed above +100%.
    """
    taken = already_taken or []
    gain = (current_price - entry_price) / entry_price
    if gain >= 2.0 and "TRIM_40" not in taken:
        return {
            "action": "TRIM_40",
            "message": "+200% — sell 40%, let 60% ride with tighter 6% trail",
            "sell_pct": 0.40,
        }
    if gain >= 1.0 and "TRIM_25" not in taken:
        return {
            "action": "TRIM_25",
            "message": "+100% — sell 25%, keep 75% with trailing stop protecting gains",
            "sell_pct": 0.25,
        }
    return None
