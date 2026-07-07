"""
Data Staleness Protocol [GAP 1]
GREEN (0-7 days): full confidence
YELLOW (7-30 days): -0.5 conviction, -1 confidence
RED (30-90 days): -1.0 conviction, -2 confidence; blocked if 2+ red flags
BLACK (>90 days): no score generated
"""
import datetime
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class DataQuality(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    BLACK = "BLACK"


@dataclass
class StalenessResult:
    data_quality: DataQuality
    days_stale: int
    conviction_penalty: float
    confidence_penalty: float
    should_score: bool
    status: str
    alert: Optional[str] = None


def check_data_staleness(
    ticker: str,
    last_fundamental_update: datetime.date,
    red_flag_count: int = 0,
) -> StalenessResult:
    today = datetime.date.today()
    days_stale = (today - last_fundamental_update).days

    if days_stale <= 7:
        return StalenessResult(
            data_quality=DataQuality.GREEN,
            days_stale=days_stale,
            conviction_penalty=0.0,
            confidence_penalty=0,
            should_score=True,
            status="Fresh",
        )

    if days_stale <= 30:
        return StalenessResult(
            data_quality=DataQuality.YELLOW,
            days_stale=days_stale,
            conviction_penalty=-0.5,
            confidence_penalty=-1,
            should_score=True,
            status=f"Data {days_stale} days old",
            alert=f"{ticker}: fundamentals {days_stale} days old, conviction reduced",
        )

    if days_stale <= 90:
        if red_flag_count >= 2:
            return StalenessResult(
                data_quality=DataQuality.RED,
                days_stale=days_stale,
                conviction_penalty=-1.0,
                confidence_penalty=-2,
                should_score=False,
                status=f"Data {days_stale} days old + {red_flag_count} red flags",
                alert=f"{ticker}: data too old + red flags present, no score generated",
            )
        return StalenessResult(
            data_quality=DataQuality.RED,
            days_stale=days_stale,
            conviction_penalty=-1.0,
            confidence_penalty=-2,
            should_score=True,
            status=f"Data {days_stale} days old, caution",
            alert=f"{ticker}: fundamentals {days_stale} days old, scoring with caution",
        )

    # >90 days — BLACK
    return StalenessResult(
        data_quality=DataQuality.BLACK,
        days_stale=days_stale,
        conviction_penalty=0.0,
        confidence_penalty=0,
        should_score=False,
        status=f"Data {days_stale} days old — cannot score",
        alert=f"CANNOT SCORE: {ticker} — data >90 days old. Awaiting earnings/10-Q release.",
    )


def cache_age_days_to_quality(days: int) -> DataQuality:
    if days <= 7:
        return DataQuality.GREEN
    if days <= 30:
        return DataQuality.YELLOW
    if days <= 90:
        return DataQuality.RED
    return DataQuality.BLACK


def check_feed_health(sources_available: int) -> dict:
    """Daily health check: how many of 3 key sources (SEC, yfinance, NewsAPI) are up."""
    if sources_available == 3:
        return {"status": "NORMAL", "scoring_enabled": True}
    if sources_available == 2:
        return {"status": "YELLOW — mark all scores YELLOW", "scoring_enabled": True}
    if sources_available == 1:
        return {"status": "RED — price only, no fundamentals", "scoring_enabled": False}
    return {"status": "HALT — all sources down", "scoring_enabled": False}
