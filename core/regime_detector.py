"""
Market Regime Detector [GAP 2]
3-signal voting: SPY momentum, VIX level, portfolio drawdown.
Runs daily at 9:35 AM. Drives stop sizing and position limits.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"
    RISING_RATES = "RISING_RATES"
    VIX_SPIKE = "VIX_SPIKE"


@dataclass
class RegimeResult:
    regime: MarketRegime
    spy_ytd_return: float
    vix_level: float
    votes: dict           # which signals voted for which regime
    max_position_pct: float
    trailing_stop_phase3: float   # % trailing stop in phase 3
    trailing_stop_phase4: float   # % trailing stop in phase 4
    min_cash_pct: float
    new_buys_paused: bool         # True when VIX > 30 — no new BUY signals
    notes: str


def _fetch_spy_ytd_return() -> Optional[float]:
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="1y")
        if hist.empty:
            return None
        jan1_idx = hist.index[hist.index.month == 1]
        if len(jan1_idx) == 0:
            return None
        ytd_start = float(hist.loc[jan1_idx[0], "Close"])
        ytd_end = float(hist["Close"].iloc[-1])
        return (ytd_end - ytd_start) / ytd_start
    except Exception as e:
        logger.error(f"fetch_spy_ytd_return: {e}")
        return None


def _fetch_vix() -> Optional[float]:
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.error(f"fetch_vix: {e}")
        return None


def detect_regime(portfolio_drawdown_pct: float = 0.0) -> RegimeResult:
    """
    portfolio_drawdown_pct: how far portfolio is from its peak (negative = down).
    e.g. -0.12 means portfolio is 12% below peak.
    """
    spy_ytd = _fetch_spy_ytd_return()
    vix = _fetch_vix()

    spy_ytd = spy_ytd or 0.0
    vix = vix or 18.0  # default to neutral if data unavailable

    votes = {}

    # Vote 1: SPY momentum
    if spy_ytd >= 0.20:
        votes["spy"] = MarketRegime.BULL
    elif spy_ytd <= -0.10:
        votes["spy"] = MarketRegime.BEAR
    else:
        votes["spy"] = MarketRegime.NEUTRAL

    # Vote 2: VIX
    if vix < 20:
        votes["vix"] = MarketRegime.BULL
    elif vix > 30:
        votes["vix"] = MarketRegime.VIX_SPIKE
    elif vix > 25:
        votes["vix"] = MarketRegime.BEAR
    else:
        votes["vix"] = MarketRegime.NEUTRAL

    # Vote 3: Portfolio drawdown
    if portfolio_drawdown_pct <= -0.25:
        votes["drawdown"] = MarketRegime.BEAR
    elif portfolio_drawdown_pct <= -0.15:
        votes["drawdown"] = MarketRegime.NEUTRAL
    else:
        votes["drawdown"] = MarketRegime.BULL

    # Resolve: majority wins; BEAR overrides on 2+ signals
    vote_counts = {}
    for v in votes.values():
        vote_counts[v] = vote_counts.get(v, 0) + 1

    if vix > 30:
        regime = MarketRegime.VIX_SPIKE
    elif vote_counts.get(MarketRegime.BEAR, 0) >= 2:
        regime = MarketRegime.BEAR
    elif vote_counts.get(MarketRegime.BULL, 0) >= 2:
        regime = MarketRegime.BULL
    else:
        regime = MarketRegime.NEUTRAL

    # Derive position parameters from regime
    if regime == MarketRegime.BULL:
        max_position_pct = 0.07
        trailing_stop_phase3 = 0.10
        trailing_stop_phase4 = 0.08
        min_cash_pct = 0.10
        notes = "Bull market: full position sizing, standard stops"
    elif regime == MarketRegime.BEAR:
        max_position_pct = 0.035   # 50% reduction
        trailing_stop_phase3 = 0.08
        trailing_stop_phase4 = 0.06
        min_cash_pct = 0.20
        notes = "Bear market: halved positions, tighter stops, 20% min cash"
    elif regime == MarketRegime.VIX_SPIKE:
        max_position_pct = 0.05    # 30% reduction
        trailing_stop_phase3 = 0.10
        trailing_stop_phase4 = 0.08
        min_cash_pct = 0.20
        notes = "VIX spike: reduce all positions 30%, increase cash buffer"
    else:
        max_position_pct = 0.055
        trailing_stop_phase3 = 0.10
        trailing_stop_phase4 = 0.08
        min_cash_pct = 0.12
        notes = "Neutral: moderate sizing"

    new_buys_paused = regime == MarketRegime.VIX_SPIKE

    return RegimeResult(
        regime=regime,
        spy_ytd_return=spy_ytd,
        vix_level=vix,
        votes=votes,
        max_position_pct=max_position_pct,
        trailing_stop_phase3=trailing_stop_phase3,
        trailing_stop_phase4=trailing_stop_phase4,
        min_cash_pct=min_cash_pct,
        new_buys_paused=new_buys_paused,
        notes=notes,
    )


# ── GAP 7: VIX spike position response ───────────────────────────────────────

@dataclass
class VixSpikeAction:
    ticker: str
    action: str           # "TRIM_30" | "HOLD" | "TIGHTEN_STOP"
    trim_pct: float       # fraction to sell (0.30 for TRIM_30, else 0.0)
    urgency: str          # "END_OF_DAY" | "MONITOR"
    rationale: str
    alert: str


def get_vix_spike_actions(
    held_positions: dict[str, float],   # {ticker: position_pct}
    regime: Optional[RegimeResult] = None,
) -> list[VixSpikeAction]:
    """
    GAP 7: When VIX > 30, generate TRIM_30 actions for all held positions.
    Positions already small (<= 2%) get TIGHTEN_STOP instead of trim.
    Returns empty list when regime is not VIX_SPIKE.

    held_positions: {ticker: current portfolio weight as fraction 0-1}
    """
    regime = regime or detect_regime()

    if regime.regime != MarketRegime.VIX_SPIKE:
        return []

    actions: list[VixSpikeAction] = []
    vix = regime.vix_level

    for ticker, pct in held_positions.items():
        if pct <= 0.02:
            # Small position — tighten stop instead of trimming
            actions.append(VixSpikeAction(
                ticker=ticker,
                action="TIGHTEN_STOP",
                trim_pct=0.0,
                urgency="MONITOR",
                rationale=(
                    f"VIX {vix:.0f}: position already small ({pct:.1%}) — "
                    "tighten stop by 0.5× ATR, no trim required"
                ),
                alert=(
                    f"{ticker} ({pct:.1%}): VIX SPIKE — tighten stop, hold position"
                ),
            ))
        else:
            actions.append(VixSpikeAction(
                ticker=ticker,
                action="TRIM_30",
                trim_pct=0.30,
                urgency="END_OF_DAY",
                rationale=(
                    f"VIX {vix:.0f}: reduce {ticker} by 30% ({pct:.1%} → {pct*0.70:.1%}) "
                    "to rebuild cash buffer during volatility spike"
                ),
                alert=(
                    f"TRIM 30% {ticker}: VIX SPIKE ({vix:.0f}) — "
                    f"sell 30% by EOD, raise cash to {regime.min_cash_pct:.0%} floor"
                ),
            ))

    actions.sort(key=lambda a: (-a.trim_pct, a.ticker))
    return actions
