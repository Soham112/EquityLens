"""
Sentiment Agent — powered by Bigdata.com (https://bigdata.com).
Reads from BigData cache populated by workflows/bigdata_refresh.py.

Applies 4 timing filters [GAP 5]:
  Rule 1: Sentiment momentum (improving vs deteriorating vs baseline)
  Rule 2: Z-score check (unusual spike vs normal coverage)
  Rule 3: Source diversity (already handled by BigData's scoring model)
  Rule 4: Media attention momentum (accelerating = more signal weight)
"""
import logging
from dataclasses import dataclass
from typing import Optional

from core.bigdata_client import BigDataSentiment, get_sentiment

logger = logging.getLogger(__name__)


@dataclass
class SentimentResult:
    ticker: str
    sentiment_score: float       # -1.0 to +1.0
    sentiment_momentum: float    # positive = improving trend
    media_attention_momentum: float
    zscore_1mo: float
    timing_status: str           # "LEADING" | "RETROACTIVE" | "STEADY" | "SPIKE"
    source_status: str           # "OK" | "CONCENTRATED" | "INSUFFICIENT"
    sentiment_boost: float       # final adjusted boost applied to Hunter (-5 to +5)
    events: list[str]            # detected events from news
    narrative_excerpt: str       # first 300 chars of AI narrative
    summary: str
    requires_human_review: bool
    data_source: str             # "bigdata" | "none"
    sentiment_dimensions: Optional[dict] = None  # multi-dimensional sentiment scores


def _apply_timing_filters(bd: BigDataSentiment) -> tuple[str, str, float]:
    """
    Returns (timing_status, source_status, boost_multiplier).

    Rule 1: Z-score check — unusual spike vs normal
    Rule 2: Sentiment momentum direction
    Rule 3: Media attention momentum
    Rule 4: Source diversity (BigData pre-filters; we check doc count)
    """
    score = bd.sentiment_score
    momentum = bd.sentiment_momentum
    zscore = bd.zscore_1mo
    media_mom = bd.media_attention_momentum

    # Timing status
    if abs(zscore) > 2.0:
        timing_status = "SPIKE"          # unusual, likely event-driven
        boost_mult = 0.75                # reduce weight slightly — may be retroactive
    elif momentum > 0.01:
        timing_status = "LEADING"        # sentiment improving ahead of price
        boost_mult = 1.0
    elif momentum < -0.01:
        timing_status = "RETROACTIVE"    # sentiment deteriorating after move
        boost_mult = 0.5
    else:
        timing_status = "STEADY"
        boost_mult = 0.85

    # Source diversity (proxy: doc count)
    if bd.source_count >= 10:
        source_status = "OK"
    elif bd.source_count >= 5:
        source_status = "MODERATE"
        boost_mult *= 0.85
    else:
        source_status = "INSUFFICIENT"
        boost_mult *= 0.5

    # Media attention bonus: accelerating coverage = more reliable signal
    if media_mom > 20:
        boost_mult = min(boost_mult * 1.1, 1.0)

    return timing_status, source_status, boost_mult


def _score_to_boost(sentiment_score: float, boost_mult: float) -> float:
    """
    Convert BigData sentiment score (-1 to +1) → Hunter adjustment (-5 to +5).
    Negative news always applied (no timing filter on downside per spec).
    """
    if sentiment_score > 0:
        raw_boost = sentiment_score * 5 * boost_mult
    else:
        raw_boost = sentiment_score * 5   # full penalty, no multiplier reduction
    return round(max(-5.0, min(5.0, raw_boost)), 2)


def _analyst_adjustment(bd: BigDataSentiment) -> float:
    """Small additional boost from analyst event signals in news."""
    events = bd.events
    adj = 0.0
    if "earnings_beat" in events:
        adj += 0.3
    if "guidance_cut" in events:
        adj -= 0.5
    if "contract_win" in events:
        adj += 0.2
    if "regulatory_approval" in events:
        adj += 0.2
    if "litigation" in events or "sec_investigation" in events:
        adj -= 1.0
    return round(max(-1.0, min(1.0, adj)), 2)


def score_sentiment_dimensions(ticker: str, bd_data, yf_data=None) -> dict:
    """
    Multi-dimensional sentiment scoring.
    bd_data: BigDataSentiment object (or None)
    yf_data: dict with yfinance fundamentals (optional, for revenue_growth_yoy, short_float_pct)

    Returns:
    {
        "earnings_quality": float,      # -1 to +1
        "narrative_momentum": float,    # -1 to +1
        "institutional_signal": float,  # -1 to +1
        "composite": float,             # weighted average
        "notes": str
    }
    """
    yf_data = yf_data or {}
    notes_parts = []

    # ── Earnings quality: revenue growth AND earnings surprise ──
    earnings_quality = 0.0
    revenue_growth = yf_data.get("revenue_growth_yoy")
    earnings_surprise = None
    if bd_data is not None:
        earnings_surprise = getattr(bd_data, "eps_surprise_pct", None)
        if earnings_surprise is None:
            earnings_surprise = getattr(bd_data, "earnings_surprise_pct", None)

    if revenue_growth is not None and earnings_surprise is not None:
        if revenue_growth > 0.15 and earnings_surprise > 5.0:
            earnings_quality = 1.0
            notes_parts.append(f"EQ=+1 (rev_growth={revenue_growth:.0%}, surprise={earnings_surprise:+.1f}%)")
        elif revenue_growth > 0 and earnings_surprise > 0:
            earnings_quality = 0.0
            notes_parts.append(f"EQ=0 (flat growth/surprise)")
        else:
            earnings_quality = -1.0
            notes_parts.append(f"EQ=-1 (negative rev_growth or miss)")
    elif revenue_growth is not None:
        if revenue_growth > 0.15:
            earnings_quality = 0.5
        elif revenue_growth < 0:
            earnings_quality = -0.5
        notes_parts.append(f"EQ={earnings_quality:.1f} (rev_growth={revenue_growth:.0%}, no surprise data)")
    else:
        notes_parts.append("EQ=0 (no data)")

    # ── Narrative momentum: sentiment score + media attention ──
    narrative_momentum = 0.0
    if bd_data is not None:
        sentiment_score = getattr(bd_data, "sentiment_score", 0.0) or 0.0
        media_attention = getattr(bd_data, "media_attention", 50) or 50

        if sentiment_score > 0.6:
            narrative_momentum += 0.5
        elif sentiment_score < 0.3:
            narrative_momentum -= 0.5

        if media_attention > 70:
            narrative_momentum += 0.3
        elif media_attention < 30:
            narrative_momentum -= 0.3

        narrative_momentum = max(-1.0, min(1.0, narrative_momentum))
        notes_parts.append(f"NM={narrative_momentum:.2f} (sent={sentiment_score:.2f}, media={media_attention})")
    else:
        notes_parts.append("NM=0 (no BigData)")

    # ── Institutional signal: short float + insider buying ──
    institutional_signal = 0.0
    short_float = yf_data.get("short_float_pct")
    insider_buy = yf_data.get("insider_buy_recent", False)

    if short_float is not None:
        if short_float < 5.0:
            institutional_signal += 0.3  # institutions comfortable
        elif short_float > 20.0:
            institutional_signal -= 0.5  # heavy shorting = distribution

    if insider_buy:
        institutional_signal += 0.4

    # Check BigData insider events
    if bd_data is not None:
        events = getattr(bd_data, "events", []) or []
        if "insider_buying" in events:
            institutional_signal += 0.2
        if "institutional_buying" in events:
            institutional_signal += 0.2

    institutional_signal = max(-1.0, min(1.0, institutional_signal))
    notes_parts.append(f"IS={institutional_signal:.2f} (short={short_float}, insider={insider_buy})")

    # ── Composite: 40% earnings + 30% narrative + 30% institutional ──
    composite = round(
        0.40 * earnings_quality +
        0.30 * narrative_momentum +
        0.30 * institutional_signal,
        3,
    )

    return {
        "earnings_quality": round(earnings_quality, 3),
        "narrative_momentum": round(narrative_momentum, 3),
        "institutional_signal": round(institutional_signal, 3),
        "composite": composite,
        "notes": " | ".join(notes_parts),
    }


def run(
    ticker: str,
    company_name: str = "",
) -> SentimentResult:

    bd: Optional[BigDataSentiment] = get_sentiment(ticker)

    if bd is None:
        return SentimentResult(
            ticker=ticker,
            sentiment_score=0.0,
            sentiment_momentum=0.0,
            media_attention_momentum=0.0,
            zscore_1mo=0.0,
            timing_status="UNKNOWN",
            source_status="NO_DATA",
            sentiment_boost=0.0,
            events=[],
            narrative_excerpt="No BigData cache. Run: python workflows/bigdata_refresh.py",
            summary="No sentiment data — cache missing",
            requires_human_review=False,
            data_source="none",
        )

    timing_status, source_status, boost_mult = _apply_timing_filters(bd)
    base_boost = _score_to_boost(bd.sentiment_score, boost_mult)
    analyst_adj = _analyst_adjustment(bd)
    sentiment_boost = round(max(-5.0, min(5.0, base_boost + analyst_adj)), 2)

    summary = (
        f"BigData sentiment={bd.sentiment_score:.3f} (momentum={bd.sentiment_momentum:+.3f}), "
        f"media_momentum={bd.media_attention_momentum:+.1f}%, "
        f"zscore_1mo={bd.zscore_1mo:.1f}, "
        f"timing={timing_status}, sources={source_status}({bd.source_count} docs), "
        f"events={bd.events}, "
        f"final_boost={sentiment_boost:+.2f}"
    )

    dimensions = score_sentiment_dimensions(ticker, bd_data=bd)

    return SentimentResult(
        ticker=ticker,
        sentiment_score=bd.sentiment_score,
        sentiment_momentum=bd.sentiment_momentum,
        media_attention_momentum=bd.media_attention_momentum,
        zscore_1mo=bd.zscore_1mo,
        timing_status=timing_status,
        source_status=source_status,
        sentiment_boost=sentiment_boost,
        events=bd.events,
        narrative_excerpt=bd.narrative[:300] if bd.narrative else "",
        summary=summary,
        requires_human_review=bd.requires_human_review,
        data_source="bigdata",
        sentiment_dimensions=dimensions,
    )
