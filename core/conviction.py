"""
Three-Gate Conviction System [GAP 3]
Gate 1: Kill switch (litigation, SEC, auditor) → conviction = 0
Gate 2: Base score = Hunter + sentiment adj − critic penalty
Gate 3: Data confidence (separate from conviction)

Buy signal: conviction >= 8 AND data_confidence >= 7
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

KILL_SWITCH_TRIGGERS = {
    "litigation",
    "sec_investigation",
    "auditor_warning",
    "going_concern",
    "accounting_restatement",
}


@dataclass
class ConvictionResult:
    ticker: str
    kill_switch_triggered: bool
    kill_switch_reason: Optional[str]

    hunter_raw: float
    sentiment_adjustment: float
    critic_penalty: float
    staleness_penalty: float
    sector_penalty: float

    conviction: float           # 0-10, capped
    data_confidence: float      # 1-10, separate score

    signal: str                 # "BUY" | "WATCHLIST" | "AVOID"
    summary: str


def _critic_penalty(red_flag_count: int) -> float:
    if red_flag_count == 0:
        return 0.0
    if red_flag_count == 1:
        return -1.0
    if red_flag_count == 2:
        return -2.0
    return -3.5  # 3+ flags


def calculate_conviction(
    ticker: str,
    hunter_raw: float,
    red_flags: list[str],           # list of flag keys
    sentiment_adjustment: float,    # from Sentiment agent (-5 to +5)
    staleness_penalty: float,       # from staleness checker
    confidence_penalty: float,      # from staleness checker
    sector_penalty: float = 0.0,    # from Scout agent
    missing_critical_data: bool = False,
    data_source_conflict: bool = False,
    retroactive_sentiment: bool = False,
    only_tier2_sources: bool = False,
) -> ConvictionResult:

    # Gate 1: Kill switch
    kill_triggers = [f for f in red_flags if f in KILL_SWITCH_TRIGGERS]
    if kill_triggers:
        return ConvictionResult(
            ticker=ticker,
            kill_switch_triggered=True,
            kill_switch_reason=", ".join(kill_triggers),
            hunter_raw=hunter_raw,
            sentiment_adjustment=0,
            critic_penalty=0,
            staleness_penalty=0,
            sector_penalty=0,
            conviction=0,
            data_confidence=0,
            signal="AVOID",
            summary=f"KILL SWITCH TRIGGERED — DO NOT BUY ({', '.join(kill_triggers)})",
        )

    # Gate 2: Base conviction calculation
    non_kill_flags = [f for f in red_flags if f not in KILL_SWITCH_TRIGGERS]
    critic_pen = _critic_penalty(len(non_kill_flags))

    # Sentiment is a tiebreaker, not a thesis: raw boost spans ±5 on a 10-point
    # scale and was single-handedly pushing mediocre hunter scores into BUY
    # territory (BUY count tripled after each Sunday cache refresh). Clamp it.
    from config.settings import settings
    cap = settings.max_sentiment_boost
    sentiment_adjustment = max(-cap, min(cap, sentiment_adjustment))

    adjusted_hunter = max(0, min(10, hunter_raw + sentiment_adjustment))
    raw_conviction = adjusted_hunter + critic_pen + staleness_penalty + sector_penalty
    conviction = round(max(0, min(10, raw_conviction)), 1)

    # Gate 3: Data confidence
    base_confidence = 10.0
    base_confidence += confidence_penalty  # from staleness
    if data_source_conflict:
        base_confidence -= 1
    if missing_critical_data:
        base_confidence -= 2
    if only_tier2_sources:
        base_confidence -= 2
    if retroactive_sentiment:
        base_confidence -= 1
    if sector_penalty < -1:
        base_confidence -= 1
    data_confidence = round(max(1, min(10, base_confidence)), 1)

    # Signal
    if conviction >= 8 and data_confidence >= 7:
        signal = "BUY"
    elif conviction >= 6 and data_confidence >= 6:
        signal = "WATCHLIST"
    else:
        signal = "AVOID"

    summary = (
        f"Hunter={hunter_raw:.1f} + Sentiment={sentiment_adjustment:+.1f} "
        f"+ Critic={critic_pen:.1f} + Staleness={staleness_penalty:.1f} "
        f"+ Sector={sector_penalty:.1f} → Conviction={conviction:.1f}, "
        f"DataConf={data_confidence:.1f} → {signal}"
    )

    return ConvictionResult(
        ticker=ticker,
        kill_switch_triggered=False,
        kill_switch_reason=None,
        hunter_raw=hunter_raw,
        sentiment_adjustment=sentiment_adjustment,
        critic_penalty=critic_pen,
        staleness_penalty=staleness_penalty,
        sector_penalty=sector_penalty,
        conviction=conviction,
        data_confidence=data_confidence,
        signal=signal,
        summary=summary,
    )
