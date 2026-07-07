"""
Weekly Outcome Review

Runs every Sunday. For every open position (journal entry with no exit):
  1. Fetches current price from yfinance
  2. Checks stop-loss tiers — alerts if any tier is breached
  3. Checks conviction drop matrix against last week's scores
  4. Checks profit-taking levels (+50%, +100%, +200%)
  5. Checks bias checkpoints on the full journal history
  6. Writes data/weekly_review_YYYY-MM-DD.json

Also reviews SKIPPED signals: "you skipped NVDA at $X, it's now at $Y (+Z%)"
This is the learning loop — surfaces both right and wrong calls.
"""
import datetime
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.llm_client import OPUS, call_llm

from agents.journal import TradeRecord, load_records, log_decision
from core.bias_check import scan_for_biases
from core.conviction_monitor import check_conviction_drop
from core.persistence import (get_conviction_series, load_portfolio,
                               update_position)
from core.stop_loss import check_trimming_levels

logger = logging.getLogger(__name__)


@dataclass
class PositionReview:
    ticker: str
    entry_price: float
    entry_date: str
    current_price: float
    return_pct: float
    days_held: int
    stop_tier1: Optional[float]
    stop_tier2: Optional[float]
    stop_tier3: Optional[float]
    stop_status: str           # NORMAL | TIER1_ALERT | TIER2_WARNING | TIER3_HIT
    trim_recommendation: Optional[dict]  # from check_trimming_levels
    conviction_drop_alert: Optional[str]
    alerts: list[str] = field(default_factory=list)


@dataclass
class SkippedSignalReview:
    ticker: str
    signal_date: str
    model_conviction: float
    skip_reason: str
    price_at_signal: Optional[float]
    current_price: float
    hypothetical_return: Optional[float]   # what you would have made
    lesson: str                            # "Good skip" | "Missed opportunity"


@dataclass
class WeeklyReview:
    review_date: str
    open_positions: list[PositionReview]
    skipped_signals: list[SkippedSignalReview]
    bias_flags: list[dict]
    alerts: list[str]          # high-priority actions needed
    summary: str
    sector_snapshot: list[dict] = None  # sector momentum from agents/scout
    trade_stats: dict = None            # win rate / expectancy / profit factor from trade logs

    def to_dict(self) -> dict:
        return {
            "review_date": self.review_date,
            "open_positions": [vars(p) for p in self.open_positions],
            "skipped_signals": [vars(s) for s in self.skipped_signals],
            "bias_flags": self.bias_flags,
            "alerts": self.alerts,
            "summary": self.summary,
            "sector_snapshot": self.sector_snapshot or [],
            "trade_stats": self.trade_stats or {},
        }


def _fetch_current_price(ticker: str) -> Optional[float]:
    try:
        hist = yf.Ticker(ticker).history(period="2d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def _review_open_positions() -> tuple[list[PositionReview], list[str]]:
    # Merge persistence portfolio + paper trading portfolio
    portfolio = load_portfolio()

    # Pull in paper trading positions (auto-executed BUY signals)
    try:
        from core.paper_trading import load_paper_portfolio
        pp = load_paper_portfolio()
        for ticker, ppos in pp.positions.items():
            if ticker not in portfolio:
                portfolio[ticker] = {
                    "entry_price": ppos.entry_price,
                    "entry_date": ppos.entry_date,
                    "stop_tier1": ppos.stop_tier1,
                    "stop_tier2": ppos.stop_tier2,
                    "stop_tier3": ppos.stop_tier3,
                    "position_pct": ppos.recommended_pct,
                    "conviction": ppos.conviction,
                    "source": "paper",
                }
    except Exception as e:
        logger.debug(f"Paper portfolio merge skipped: {e}")

    reviews: list[PositionReview] = []
    high_priority_alerts: list[str] = []

    for ticker, pos in portfolio.items():
        current_price = _fetch_current_price(ticker)
        if current_price is None:
            logger.warning(f"{ticker}: price unavailable")
            continue

        # Update stored price
        update_position(ticker, current_price=current_price)

        entry_price = pos.get("entry_price", current_price)
        entry_date = pos.get("entry_date", "")
        ret = (current_price - entry_price) / entry_price

        days_held = 0
        if entry_date:
            try:
                days_held = (datetime.date.today() - datetime.date.fromisoformat(entry_date)).days
            except ValueError:
                pass

        t1 = pos.get("stop_tier1")
        t2 = pos.get("stop_tier2")
        t3 = pos.get("stop_tier3")

        # Stop status
        stop_status = "NORMAL"
        alerts: list[str] = []
        if t3 and current_price <= t3:
            stop_status = "TIER3_HIT"
            msg = f"STOP HIT {ticker}: price ${current_price:.2f} breached Tier 3 stop ${t3:.2f} — EXIT"
            alerts.append(msg)
            high_priority_alerts.append(msg)
        elif t2 and current_price <= t2:
            stop_status = "TIER2_WARNING"
            msg = f"STOP WARNING {ticker}: price ${current_price:.2f} near Tier 2 ${t2:.2f} — re-evaluate"
            alerts.append(msg)
            high_priority_alerts.append(msg)
        elif t1 and current_price <= t1:
            stop_status = "TIER1_ALERT"
            alerts.append(f"STOP ALERT {ticker}: price ${current_price:.2f} hit Tier 1 alert ${t1:.2f}")

        # Profit-taking
        trim_rec = check_trimming_levels(entry_price, current_price, pos.get("position_pct", 0.05))
        if trim_rec:
            msg = f"TRIM {ticker}: {trim_rec['message']}"
            alerts.append(msg)
            high_priority_alerts.append(msg)

        # Conviction drop (compare last 2 conviction snapshots)
        series = get_conviction_series(ticker, days=14)
        conv_alert = None
        if len(series) >= 2:
            prev_conv = series[-2]["conviction"]
            curr_conv = series[-1]["conviction"]
            drop = check_conviction_drop(ticker, prev_conv, curr_conv)
            if drop.alert:
                conv_alert = drop.alert
                alerts.append(drop.alert)
                if drop.action.value in ("TRIM_50", "EXIT"):
                    high_priority_alerts.append(drop.alert)

        reviews.append(PositionReview(
            ticker=ticker,
            entry_price=entry_price,
            entry_date=entry_date,
            current_price=current_price,
            return_pct=round(ret, 4),
            days_held=days_held,
            stop_tier1=t1, stop_tier2=t2, stop_tier3=t3,
            stop_status=stop_status,
            trim_recommendation=trim_rec,
            conviction_drop_alert=conv_alert,
            alerts=alerts,
        ))

    return reviews, high_priority_alerts


def _review_skipped_signals() -> list[SkippedSignalReview]:
    """
    For every SKIP journal entry in the last 60 days, fetch current price
    and calculate the hypothetical return.
    """
    records = load_records(days_back=60)
    skipped = [r for r in records if r.your_decision == "SKIP" and r.entry_date is None]
    reviews: list[SkippedSignalReview] = []

    for r in skipped:
        current_price = _fetch_current_price(r.ticker)
        if current_price is None:
            continue

        # Best proxy for price at signal: fetch from yfinance history
        price_at_signal: Optional[float] = None
        hyp_return: Optional[float] = None
        try:
            signal_date = datetime.date.fromisoformat(r.recorded_at[:10])
            end = signal_date + datetime.timedelta(days=3)
            hist = yf.Ticker(r.ticker).history(
                start=signal_date.isoformat(), end=end.isoformat()
            )
            if not hist.empty:
                price_at_signal = float(hist["Close"].iloc[0])
                hyp_return = round((current_price - price_at_signal) / price_at_signal, 4)
        except Exception:
            pass

        lesson = "Insufficient data"
        if hyp_return is not None:
            if hyp_return >= 0.15:
                lesson = f"Missed opportunity: +{hyp_return:.0%} since signal"
            elif hyp_return <= -0.10:
                lesson = f"Good skip: would have lost {hyp_return:.0%}"
            else:
                lesson = f"Neutral: {hyp_return:+.0%} — within noise"

        reviews.append(SkippedSignalReview(
            ticker=r.ticker,
            signal_date=r.recorded_at[:10],
            model_conviction=r.model_conviction,
            skip_reason=r.decision_reason or "",
            price_at_signal=price_at_signal,
            current_price=current_price,
            hypothetical_return=hyp_return,
            lesson=lesson,
        ))

    # Sort by hypothetical return descending (biggest misses first)
    reviews.sort(key=lambda x: -(x.hypothetical_return or 0))
    return reviews


_WEEKLY_REVIEW_SYSTEM = """\
Synthesize a weekly paper portfolio review into prioritized, actionable insights for a retail equity investor.

<role>
Act as a direct, honest portfolio coach. You receive structured JSON data from an automated review system \
covering open position performance, stop-loss status, skipped signal outcomes, and behavioral bias flags. \
Your job is to cut through the noise and surface exactly what matters this week — not what is merely interesting.
</role>

<reasoning_steps>
Step 1: Scan for immediate action items first — stop hits (TIER3_HIT), trim triggers, and large conviction drops. \
        If any exist, they lead the response regardless of everything else.
Step 2: Assess the skipped signal outcomes. Identify the single most instructive case — \
        either the biggest missed opportunity or the best-validated skip. Explain WHAT the signal was saying, \
        not just the return number.
Step 3: Evaluate behavioral bias flags. Determine if they represent a one-off or a pattern. \
        Name the specific cognitive trap and why it is dangerous in the current market context.
Step 4: Identify 1-2 concrete things to watch or act on next week based on the full picture.
</reasoning_steps>

<output_format>
Respond in exactly 4 short paragraphs with these bold headers:

**Portfolio Health** — Overall status: number of open positions, combined P&L direction, \
any stop actions required. If no stops were hit, say so directly.

**This Week's Lesson** — The single most instructive skipped signal or position outcome. \
Include the ticker, the original signal strength, and what the price action since then reveals \
about what was right or wrong in the analysis.

**Bias Watch** — Name each behavioral bias flagged. Explain the specific risk it creates for \
this investor's decision-making right now. If no biases were flagged, confirm that and note \
what discipline is keeping the process clean.

**Next Week Focus** — 1-2 specific, concrete action items: tickers to watch, levels to monitor, \
or process adjustments to make. No generic advice.
</output_format>

<output_requirements>
- Maximum 250 words total
- Use specific ticker names, prices, and percentages from the data — never speak in generalities
- Tone: direct coach, not cheerleader — be honest about mistakes and missed calls
- Do NOT start any sentence with "It is worth noting", "It should be mentioned", or similar filler
- If data is missing or positions list is empty, say so plainly and focus on what data exists
</output_requirements>

<example>
<input_summary>
Open positions: NVDA +34% (68d), MSFT -4% (12d). Stop hit: none. \
Skipped: META at $520 (conviction 8.1) — now $618 (+18.8%). Bias: LOSS_AVERSION flagged on MSFT hold.
</input_summary>
<output>
**Portfolio Health** — Two open positions, one strong winner (NVDA +34%) and one early-stage position \
(MSFT -4% in 12 days). No stops hit this week; all tiers intact.

**This Week's Lesson** — Skipping META at $520 with a conviction of 8.1 cost +18.8% in 4 weeks. \
The signal had strong fundamentals and a Stage 2 breakout — the skip was not justified by the data. \
This is the kind of high-conviction signal that should be taken, not second-guessed.

**Bias Watch** — Loss aversion is flagged on MSFT: holding a -4% position and mentally anchoring \
to breakeven rather than re-evaluating the thesis. If the fundamentals still hold, hold it. \
If the reason for buying has changed, the entry price is irrelevant.

**Next Week Focus** — Re-evaluate META: if it pulls back to $580-590, the original thesis is intact \
and a second look is warranted. On MSFT, check whether the conviction score has moved — \
if it drops below 6, that is the exit signal, not price anchoring.
</output>
</example>"""


def _generate_opus_narrative(review: "WeeklyReview") -> Optional[str]:
    """Call Opus to synthesize the week's structured data into an actionable narrative."""
    positions_summary = [
        {
            "ticker": p.ticker,
            "return_pct": f"{p.return_pct:+.1%}",
            "days_held": p.days_held,
            "stop_status": p.stop_status,
            "alerts": p.alerts,
            "trim_recommendation": p.trim_recommendation,
        }
        for p in review.open_positions
    ]
    skips_summary = [
        {
            "ticker": s.ticker,
            "signal_date": s.signal_date,
            "conviction": s.model_conviction,
            "hypothetical_return": f"{s.hypothetical_return:+.1%}" if s.hypothetical_return is not None else "N/A",
            "lesson": s.lesson,
        }
        for s in review.skipped_signals[:10]
    ]
    payload = {
        "review_date": review.review_date,
        "open_positions": positions_summary,
        "skipped_signals": skips_summary,
        "bias_flags": review.bias_flags,
        "high_priority_alerts": review.alerts,
        "sector_momentum": review.sector_snapshot or [],
    }
    user_msg = (
        "Generate the weekly portfolio review narrative for this data.\n\n"
        "<review_data>\n"
        f"{json.dumps(payload, indent=2)}\n"
        "</review_data>"
    )
    return call_llm(
        system=_WEEKLY_REVIEW_SYSTEM,
        user=user_msg,
        model=OPUS,
        max_tokens=400,
    )


def _build_sector_snapshot() -> list[dict]:
    """Fetch live sector momentum for all tracked sectors, sorted by 60d return."""
    try:
        from agents.scout import scan_all_sectors
        sectors = scan_all_sectors()
        result = []
        for name, s in sorted(sectors.items(), key=lambda x: (x[1].return_60d or 0), reverse=True):
            result.append({
                "sector": name,
                "etf": s.etf,
                "return_20d": round(s.return_20d, 4) if s.return_20d is not None else None,
                "return_60d": round(s.return_60d, 4) if s.return_60d is not None else None,
                "return_120d": round(s.return_120d, 4) if s.return_120d is not None else None,
                "rotation_rank": s.rotation_rank,
                "status": s.status,
                "signal": s.signal,
                "crowding_score": round(s.crowding_score, 2),
                "notes": s.notes,
            })
        return result
    except Exception as e:
        logger.warning(f"Sector snapshot failed: {e}")
        return []


def run_weekly_review() -> WeeklyReview:
    today = datetime.date.today().isoformat()
    logger.info(f"Running weekly outcome review for {today}...")

    position_reviews, high_priority = _review_open_positions()
    skipped_reviews = _review_skipped_signals()
    bias_report = scan_for_biases(journal_lookback_days=60)
    bias_flags = [f.to_dict() if hasattr(f, 'to_dict') else vars(f)
                  for f in bias_report.flags] if bias_report.flags else []

    logger.info("Fetching sector momentum snapshot...")
    sector_snapshot = _build_sector_snapshot()

    # Build summary
    lines = [f"Weekly Review — {today}", ""]

    # Sector momentum section
    if sector_snapshot:
        lines.append("Sector Momentum:")
        for s in sector_snapshot:
            r20 = f"{s['return_20d']:+.1%}" if s['return_20d'] is not None else "n/a"
            r60 = f"{s['return_60d']:+.1%}" if s['return_60d'] is not None else "n/a"
            icon = "🟢" if s['rotation_rank'] == "LEADING" else "🟡" if s['rotation_rank'] == "MIDDLE" else "🔴"
            lines.append(f"  {icon} {s['sector']:22} {s['etf']:5} | 20d={r20:8} 60d={r60:8} | {s['rotation_rank']}")
        lines.append("")

    if position_reviews:
        lines.append(f"Open Positions ({len(position_reviews)}):")
        for p in sorted(position_reviews, key=lambda x: -x.return_pct):
            status_icon = "🔴" if p.stop_status == "TIER3_HIT" else "🟡" if "TIER" in p.stop_status else "🟢"
            lines.append(f"  {status_icon} {p.ticker:6} {p.return_pct:+.1%} ({p.days_held}d)")
            for a in p.alerts:
                lines.append(f"     → {a}")
    else:
        lines.append("No open positions tracked.")

    lines.append("")
    missed = [s for s in skipped_reviews if (s.hypothetical_return or 0) >= 0.15]
    good_skips = [s for s in skipped_reviews if (s.hypothetical_return or 0) <= -0.10]
    if missed:
        lines.append(f"Missed opportunities ({len(missed)}):")
        for s in missed[:5]:
            lines.append(f"  {s.ticker}: skipped at ${s.price_at_signal:.2f}, now ${s.current_price:.2f} ({s.hypothetical_return:+.0%})")
    if good_skips:
        lines.append(f"Good skips ({len(good_skips)}):")
        for s in good_skips[:5]:
            lines.append(f"  {s.ticker}: skipped at ${s.price_at_signal:.2f}, now ${s.current_price:.2f} ({s.hypothetical_return:+.0%}) ✓")

    if not bias_report.clean:
        lines.append("")
        lines.append("Behavioral bias flags:")
        for f in bias_report.flags:
            lines.append(f"  [{f.severity}] {f.bias}: {f.message[:80]}")

    # Trade-level stats: the "is this tradeable" numbers, distinct from signal hit rates
    trade_stats = {}
    try:
        from core.trade_stats import compute_trade_stats, format_trade_stats
        trade_stats = compute_trade_stats()
        lines.append("")
        lines.extend(format_trade_stats(trade_stats))
    except Exception as e:
        logger.debug(f"trade stats skipped: {e}")

    if high_priority:
        lines.append("")
        lines.append("ACTION REQUIRED:")
        for a in high_priority:
            lines.append(f"  ⚠  {a}")

    fallback_summary = "\n".join(lines)

    review = WeeklyReview(
        review_date=today,
        open_positions=position_reviews,
        skipped_signals=skipped_reviews,
        bias_flags=bias_flags,
        alerts=high_priority,
        summary=fallback_summary,
        sector_snapshot=sector_snapshot,
        trade_stats=trade_stats,
    )

    logger.info("Generating Opus narrative for weekly review...")
    opus_narrative = _generate_opus_narrative(review)
    if opus_narrative:
        review.summary = opus_narrative
        logger.info("Opus narrative generated successfully.")

    # Save
    os.makedirs("data", exist_ok=True)
    path = os.path.join("data", f"weekly_review_{today}.json")
    with open(path, "w") as f:
        json.dump(review.to_dict(), f, indent=2)
    logger.info(f"Weekly review saved → {path}")

    return review


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    review = run_weekly_review()
    print("\n" + "=" * 60)
    print(review.summary)
    print("=" * 60)
