"""
Conviction Drop Response Matrix [GAP 12]

When a held position's conviction score drops between scans, this module
determines the required action:

  Drop 1-2 pts  → TRIM 25%   — thesis weakening, reduce exposure
  Drop 3-4 pts  → TRIM 50%   — significant deterioration
  Drop 5+ pts   → EXIT       — thesis broken, exit position
  Conviction < 6 → WATCHLIST (even if drop is small)

Also flags when a BUY position drops below the BUY threshold (8.0)
without a large drop — forces a "no add" lock until conviction recovers.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class DropAction(str, Enum):
    HOLD = "HOLD"           # no material change
    NO_ADD = "NO_ADD"       # below BUY threshold — hold existing, no new shares
    TRIM_25 = "TRIM_25"     # sell 25% of position
    TRIM_50 = "TRIM_50"     # sell 50% of position
    EXIT = "EXIT"           # exit full position


@dataclass
class ConvictionDropResult:
    ticker: str
    prev_conviction: float
    current_conviction: float
    drop: float             # positive = drop, negative = improvement
    action: DropAction
    urgency: str            # "IMMEDIATE" | "END_OF_DAY" | "MONITOR"
    rationale: str
    alert: Optional[str]


def check_conviction_drop(
    ticker: str,
    prev_conviction: float,
    current_conviction: float,
) -> ConvictionDropResult:
    """
    Compare yesterday's conviction to today's and return required action.
    Call this for every held position during the daily scan.
    """
    drop = prev_conviction - current_conviction  # positive means conviction fell

    # Improvement — always good
    if drop <= 0:
        return ConvictionDropResult(
            ticker=ticker,
            prev_conviction=prev_conviction,
            current_conviction=current_conviction,
            drop=drop,
            action=DropAction.HOLD,
            urgency="MONITOR",
            rationale=f"Conviction improved {abs(drop):.1f} pts ({prev_conviction:.1f} → {current_conviction:.1f})",
            alert=None,
        )

    # Exit: 5+ pt drop or conviction collapsed below 4
    if drop >= 5.0 or current_conviction < 4.0:
        return ConvictionDropResult(
            ticker=ticker,
            prev_conviction=prev_conviction,
            current_conviction=current_conviction,
            drop=drop,
            action=DropAction.EXIT,
            urgency="IMMEDIATE",
            rationale=(
                f"Conviction collapsed {drop:.1f} pts ({prev_conviction:.1f} → {current_conviction:.1f}) "
                "— thesis broken"
            ),
            alert=(
                f"EXIT {ticker}: conviction dropped {drop:.1f} pts to {current_conviction:.1f}. "
                "Thesis broken — exit full position immediately."
            ),
        )

    # Heavy trim: 3-4 pt drop
    if drop >= 3.0:
        return ConvictionDropResult(
            ticker=ticker,
            prev_conviction=prev_conviction,
            current_conviction=current_conviction,
            drop=drop,
            action=DropAction.TRIM_50,
            urgency="END_OF_DAY",
            rationale=(
                f"Conviction dropped {drop:.1f} pts ({prev_conviction:.1f} → {current_conviction:.1f}) "
                "— significant deterioration"
            ),
            alert=(
                f"TRIM 50% {ticker}: conviction dropped {drop:.1f} pts to {current_conviction:.1f}. "
                "Sell half by end of day."
            ),
        )

    # Light trim: 1-2 pt drop AND conviction still reasonable (6+)
    if drop >= 1.0 and current_conviction >= 6.0:
        return ConvictionDropResult(
            ticker=ticker,
            prev_conviction=prev_conviction,
            current_conviction=current_conviction,
            drop=drop,
            action=DropAction.TRIM_25,
            urgency="END_OF_DAY",
            rationale=(
                f"Conviction dropped {drop:.1f} pts ({prev_conviction:.1f} → {current_conviction:.1f}) "
                "— thesis weakening"
            ),
            alert=(
                f"TRIM 25% {ticker}: conviction dropped {drop:.1f} pts to {current_conviction:.1f}. "
                "Reduce position by 25% — monitor closely."
            ),
        )

    # Small drop but conviction fell below BUY threshold (was a BUY, now WATCHLIST)
    if prev_conviction >= 8.0 and current_conviction < 8.0:
        return ConvictionDropResult(
            ticker=ticker,
            prev_conviction=prev_conviction,
            current_conviction=current_conviction,
            drop=drop,
            action=DropAction.NO_ADD,
            urgency="MONITOR",
            rationale=(
                f"Conviction slipped below BUY threshold "
                f"({prev_conviction:.1f} → {current_conviction:.1f}) — hold, do not add"
            ),
            alert=(
                f"NO ADD {ticker}: conviction {current_conviction:.1f} — "
                "hold existing position but do not add shares until conviction recovers above 8."
            ),
        )

    # Small drop, still above thresholds — monitor only
    return ConvictionDropResult(
        ticker=ticker,
        prev_conviction=prev_conviction,
        current_conviction=current_conviction,
        drop=drop,
        action=DropAction.HOLD,
        urgency="MONITOR",
        rationale=f"Minor drop {drop:.1f} pts ({prev_conviction:.1f} → {current_conviction:.1f}) — within normal range",
        alert=None,
    )


def scan_held_positions(
    current_scores: dict[str, float],
    previous_scores: dict[str, float],
) -> list[ConvictionDropResult]:
    """
    Batch check all held positions.
    current_scores / previous_scores: {ticker: conviction_float}
    Returns list of results requiring action (TRIM or EXIT only).
    """
    actions = []
    for ticker, current in current_scores.items():
        prev = previous_scores.get(ticker, current)  # no history = no change
        result = check_conviction_drop(ticker, prev, current)
        if result.action not in (DropAction.HOLD, DropAction.NO_ADD) or result.action == DropAction.NO_ADD:
            actions.append(result)
    return sorted(actions, key=lambda r: r.drop, reverse=True)
