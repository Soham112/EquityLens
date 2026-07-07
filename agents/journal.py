"""
Journal Agent — Log decisions, outcomes, error types. Detect drift [GAP 6].
Feeds back to improve model: hit rate, payoff ratio, false positive rate.
"""
import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger(__name__)

JOURNAL_FILE = os.path.join("data", "journal.jsonl")


@dataclass
class TradeRecord:
    ticker: str
    recorded_at: str                    # ISO datetime
    model_conviction: float
    model_signal: str                   # BUY | WATCHLIST | AVOID
    your_decision: str                  # BUY | WATCH | SKIP
    decision_reason: str
    entry_price: Optional[float]
    entry_date: Optional[str]
    stop_tier1: Optional[float]
    stop_tier2: Optional[float]
    stop_tier3: Optional[float]
    exit_price: Optional[float]
    exit_date: Optional[str]
    exit_trigger: Optional[str]         # THESIS_BREAK | STOP | VALUATION | REBALANCE
    return_pct: Optional[float]
    thesis_outcome: Optional[str]       # PLAYED_OUT | BROKEN | INCOMPLETE
    error_type: Optional[str]           # VALUATION | TIMING | DATA | THESIS | MACRO | WHIPSAW
    model_lesson: Optional[str]


def log_decision(record: TradeRecord) -> None:
    os.makedirs("data", exist_ok=True)
    with open(JOURNAL_FILE, "a") as f:
        f.write(json.dumps(asdict(record)) + "\n")
    logger.info(f"Journal: logged {record.ticker} — {record.your_decision}")


def load_records(days_back: int = 90) -> list[TradeRecord]:
    if not os.path.exists(JOURNAL_FILE):
        return []
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days_back)
    records = []
    with open(JOURNAL_FILE) as f:
        for line in f:
            try:
                d = json.loads(line.strip())
                rec = TradeRecord(**d)
                if rec.recorded_at and rec.recorded_at >= cutoff.isoformat():
                    records.append(rec)
            except Exception:
                pass
    return records


@dataclass
class DriftReport:
    period_days: int
    total_closed: int
    hit_rate: float             # % that gained >0
    avg_winner_pct: float
    avg_loser_pct: float
    payoff_ratio: float         # avg_winner / avg_loser
    false_positive_rate: float  # conviction 8+ that fell >20%
    drift_alert: bool           # hit rate dropped >3% vs 30 days ago
    drift_delta: float
    summary: str


def calculate_drift(records: list[TradeRecord]) -> DriftReport:
    closed = [r for r in records if r.return_pct is not None]
    if not closed:
        return DriftReport(
            period_days=30, total_closed=0, hit_rate=0, avg_winner_pct=0,
            avg_loser_pct=0, payoff_ratio=0, false_positive_rate=0,
            drift_alert=False, drift_delta=0,
            summary="No closed trades to analyze",
        )

    winners = [r for r in closed if r.return_pct > 0]
    losers = [r for r in closed if r.return_pct <= 0]
    hit_rate = len(winners) / len(closed)
    avg_winner = sum(r.return_pct for r in winners) / len(winners) if winners else 0
    avg_loser = abs(sum(r.return_pct for r in losers) / len(losers)) if losers else 0.001
    payoff_ratio = avg_winner / avg_loser

    high_conv = [r for r in closed if r.model_conviction >= 8]
    false_positives = [r for r in high_conv if r.return_pct is not None and r.return_pct < -0.20]
    fp_rate = len(false_positives) / len(high_conv) if high_conv else 0

    # Drift: compare to 30 days ago (simple: split records in half)
    mid = len(closed) // 2
    recent_hr = len([r for r in closed[mid:] if r.return_pct > 0]) / max(len(closed[mid:]), 1)
    older_hr = len([r for r in closed[:mid] if r.return_pct > 0]) / max(len(closed[:mid]), 1)
    drift_delta = recent_hr - older_hr
    drift_alert = drift_delta < -0.03

    summary = (
        f"Hit rate: {hit_rate:.0%}, Payoff: {payoff_ratio:.1f}x, "
        f"False positives: {fp_rate:.0%}"
        + (" ⚠ DRIFT DETECTED" if drift_alert else "")
    )

    return DriftReport(
        period_days=30,
        total_closed=len(closed),
        hit_rate=hit_rate,
        avg_winner_pct=avg_winner,
        avg_loser_pct=avg_loser,
        payoff_ratio=payoff_ratio,
        false_positive_rate=fp_rate,
        drift_alert=drift_alert,
        drift_delta=drift_delta,
        summary=summary,
    )
