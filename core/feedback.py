"""
Feedback Loop — learns which screens and patterns actually work over time.

Three components:
  1. ScreenPerformanceTracker  — per-screen hit rate, avg return, kill threshold
  2. MistakePatternDetector    — finds repeated errors across journal records
  3. OutcomeLogger             — wires signal → outcome into journal at exit time

Data file: data/screen_performance.json
  One entry per (screen_name, signal_date) with outcome filled in on exit.

Usage:
  # At signal time (daily scan)
  from core.feedback import log_signal_outcome
  log_signal_outcome(ticker, screens_matched, entry_price, hunter_score, rsi)

  # At exit time (position closed)
  from core.feedback import record_exit
  record_exit(ticker, exit_price, exit_date, exit_reason)

  # For the weekly review
  from core.feedback import screen_report, mistake_report
  report = screen_report()          # per-screen hit rates
  mistakes = mistake_report()       # recurring error patterns
"""
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

PERF_FILE = os.path.join("data", "screen_performance.json")
MISTAKE_FILE = os.path.join("data", "mistake_log.json")

# A screen is flagged for review if hit rate drops below this after 10+ trades
SCREEN_KILL_THRESHOLD = 0.45
SCREEN_MIN_TRADES = 10


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SignalRecord:
    """One per signal — created at scan time, outcome filled on exit."""
    ticker: str
    signal_date: str                  # ISO date
    screens_matched: list[str]        # which screens flagged this
    signal_type: str                  # "SWING" | "MOMENTUM" | "LONG" | "BUY"
    entry_price: float
    hunter_score: float
    rsi_at_entry: float
    pattern: str                      # vision pattern (or "none")
    pattern_confidence: float
    # Filled on exit
    exit_price: Optional[float] = None
    exit_date: Optional[str] = None
    exit_reason: Optional[str] = None
    return_pct: Optional[float] = None
    hold_days: Optional[int] = None
    outcome: Optional[str] = None     # "WIN" | "LOSS" | "SCRATCH" | "OPEN"


@dataclass
class ScreenStats:
    screen_name: str
    total_signals: int
    closed_signals: int
    wins: int
    losses: int
    hit_rate: float                   # wins / closed
    avg_return_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    payoff_ratio: float               # avg_win / avg_loss
    status: str                       # "PERFORMING" | "WATCH" | "KILL"
    note: str


@dataclass
class MistakePattern:
    pattern_id: str
    description: str
    occurrences: int
    avg_loss_pct: float
    common_conditions: list[str]      # e.g. ["RSI > 70 at entry", "earnings within 2 weeks"]
    recommendation: str
    severity: str                     # "INFO" | "WARN" | "ALERT"
    examples: list[str]               # ticker examples


# ── IO ────────────────────────────────────────────────────────────────────────

def _load_records() -> list[SignalRecord]:
    if not os.path.exists(PERF_FILE):
        return []
    try:
        with open(PERF_FILE) as f:
            data = json.load(f)
        return [SignalRecord(**r) for r in data]
    except Exception as e:
        logger.error(f"[Feedback] Load failed: {e}")
        return []


def _save_records(records: list[SignalRecord]) -> None:
    os.makedirs("data", exist_ok=True)
    with open(PERF_FILE, "w") as f:
        json.dump([asdict(r) for r in records], f, indent=2)


def _load_mistakes() -> list[MistakePattern]:
    if not os.path.exists(MISTAKE_FILE):
        return []
    try:
        with open(MISTAKE_FILE) as f:
            data = json.load(f)
        return [MistakePattern(**m) for m in data]
    except Exception:
        return []


def _save_mistakes(mistakes: list[MistakePattern]) -> None:
    os.makedirs("data", exist_ok=True)
    with open(MISTAKE_FILE, "w") as f:
        json.dump([asdict(m) for m in mistakes], f, indent=2)


# ── Signal logging ─────────────────────────────────────────────────────────────

def log_signal(
    ticker: str,
    screens_matched: list[str],
    signal_type: str,
    entry_price: float,
    hunter_score: float,
    rsi_at_entry: float,
    pattern: str = "none",
    pattern_confidence: float = 0.0,
) -> None:
    """
    Call at scan time when a signal is generated.
    Creates an OPEN record — outcome filled in later via record_exit().
    """
    records = _load_records()

    # Deduplicate — a held name re-emits its BUY signal every scan day; one
    # OPEN record per ticker until record_exit closes it, else stats double-count
    today = date.today().isoformat()
    existing = [r for r in records if r.ticker == ticker and r.outcome == "OPEN"]
    if existing:
        logger.debug(f"[Feedback] {ticker} already has an open record, skipping")
        return

    record = SignalRecord(
        ticker=ticker,
        signal_date=today,
        screens_matched=screens_matched,
        signal_type=signal_type,
        entry_price=entry_price,
        hunter_score=hunter_score,
        rsi_at_entry=rsi_at_entry,
        pattern=pattern,
        pattern_confidence=pattern_confidence,
        outcome="OPEN",
    )
    records.append(record)
    _save_records(records)
    logger.info(f"[Feedback] Logged signal: {ticker} screens={screens_matched}")


def record_exit(
    ticker: str,
    exit_price: float,
    exit_reason: str,
    signal_date: Optional[str] = None,
    entry_price: Optional[float] = None,
) -> bool:
    """
    Fill in the outcome on an open signal record.
    Call when a swing/long-term position is exited.
    entry_price: actual fill price from the position — repairs records whose
    scan-time entry was the 0.0 placeholder.
    """
    records = _load_records()
    today = date.today().isoformat()

    # Find the most recent open record for this ticker
    target = None
    for r in reversed(records):
        if r.ticker == ticker and r.outcome == "OPEN":
            if signal_date is None or r.signal_date == signal_date:
                target = r
                break

    if target is None:
        logger.warning(f"[Feedback] No open signal found for {ticker}")
        return False

    if target.entry_price <= 0 and entry_price and entry_price > 0:
        target.entry_price = entry_price
    if target.entry_price <= 0:
        logger.warning(f"[Feedback] {ticker} record has no entry price — cannot score exit")
        return False

    return_pct = (exit_price - target.entry_price) / target.entry_price
    try:
        entry_d = date.fromisoformat(target.signal_date)
        hold_days = (date.fromisoformat(today) - entry_d).days
    except Exception:
        hold_days = None

    target.exit_price = exit_price
    target.exit_date = today
    target.exit_reason = exit_reason
    target.return_pct = round(return_pct, 4)
    target.hold_days = hold_days
    target.outcome = "WIN" if return_pct > 0.02 else "LOSS" if return_pct < -0.02 else "SCRATCH"

    _save_records(records)
    logger.info(
        f"[Feedback] Exit recorded: {ticker} {target.outcome} "
        f"{return_pct:+.1%} in {hold_days}d — {exit_reason}"
    )

    # Immediately re-scan for mistake patterns after each exit
    _update_mistake_patterns(records)
    return True


def update_entry_price(ticker: str, price: float) -> bool:
    """
    Fill the actual fill price on the open record for ticker.
    Called at paper-trade entry time — scan-time records are created with a
    0.0 placeholder because AnalysisResult carries no price.
    """
    if not price or price <= 0:
        return False
    records = _load_records()
    for r in reversed(records):
        if r.ticker == ticker and r.outcome == "OPEN" and r.entry_price <= 0:
            r.entry_price = price
            _save_records(records)
            logger.info(f"[Feedback] Entry price filled: {ticker} @ ${price:.2f}")
            return True
    return False


# ── Screen performance report ─────────────────────────────────────────────────

def screen_report(min_trades: int = 5) -> list[ScreenStats]:
    """
    Per-screen hit rate and return statistics.
    Only includes screens with >= min_trades closed signals.
    """
    records = _load_records()
    closed = [r for r in records if r.outcome in ("WIN", "LOSS", "SCRATCH")]

    # Group by screen
    screen_records: dict[str, list[SignalRecord]] = {}
    for r in records:
        for s in r.screens_matched:
            screen_records.setdefault(s, []).append(r)

    stats = []
    for screen_name, recs in screen_records.items():
        total = len(recs)
        closed_recs = [r for r in recs if r.outcome in ("WIN", "LOSS", "SCRATCH")]
        if len(closed_recs) < min_trades:
            continue

        wins = [r for r in closed_recs if r.outcome == "WIN"]
        losses = [r for r in closed_recs if r.outcome == "LOSS"]
        hit_rate = len(wins) / len(closed_recs)
        avg_ret = sum(r.return_pct for r in closed_recs) / len(closed_recs)
        avg_win = sum(r.return_pct for r in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(r.return_pct for r in losses) / len(losses)) if losses else 0.001
        payoff = avg_win / avg_loss

        if hit_rate < SCREEN_KILL_THRESHOLD and len(closed_recs) >= SCREEN_MIN_TRADES:
            status = "KILL"
            note = f"Hit rate {hit_rate:.0%} below {SCREEN_KILL_THRESHOLD:.0%} threshold — consider disabling"
        elif hit_rate < 0.50:
            status = "WATCH"
            note = f"Hit rate {hit_rate:.0%} marginal — monitor closely"
        else:
            status = "PERFORMING"
            note = f"Solid — hit rate {hit_rate:.0%}, payoff {payoff:.1f}x"

        stats.append(ScreenStats(
            screen_name=screen_name,
            total_signals=total,
            closed_signals=len(closed_recs),
            wins=len(wins),
            losses=len(losses),
            hit_rate=round(hit_rate, 3),
            avg_return_pct=round(avg_ret, 4),
            avg_win_pct=round(avg_win, 4),
            avg_loss_pct=round(avg_loss, 4),
            payoff_ratio=round(payoff, 2),
            status=status,
            note=note,
        ))

    stats.sort(key=lambda s: -s.hit_rate)
    return stats


# ── Mistake pattern detector ──────────────────────────────────────────────────

def _update_mistake_patterns(records: list[SignalRecord]) -> None:
    """
    Scan closed losing trades for recurring conditions.
    Updates mistake_log.json with found patterns.
    """
    losses = [r for r in records if r.outcome == "LOSS" and r.return_pct is not None]
    if len(losses) < 3:
        return

    mistakes: list[MistakePattern] = []

    # ── Pattern 1: High RSI at entry ──
    rsi_losses = [r for r in losses if r.rsi_at_entry > 68]
    if len(rsi_losses) >= 2:
        avg_loss = sum(r.return_pct for r in rsi_losses) / len(rsi_losses)
        mistakes.append(MistakePattern(
            pattern_id="high_rsi_entry",
            description="Entering when RSI > 68 (overbought territory)",
            occurrences=len(rsi_losses),
            avg_loss_pct=round(avg_loss, 4),
            common_conditions=[f"RSI avg {sum(r.rsi_at_entry for r in rsi_losses)/len(rsi_losses):.0f} at entry"],
            recommendation="Wait for RSI to pull back below 60 before entering. Overbought = late to the party.",
            severity="WARN" if len(rsi_losses) < 4 else "ALERT",
            examples=[r.ticker for r in rsi_losses[:3]],
        ))

    # ── Pattern 2: Low Hunter score swing trades ──
    # hunter_score 0.0 = unknown (backfilled/legacy records) — must not count as "low"
    low_score_swings = [r for r in losses if r.signal_type in ("SWING", "MOMENTUM") and 0 < r.hunter_score < 5.5]
    if len(low_score_swings) >= 2:
        avg_loss = sum(r.return_pct for r in low_score_swings) / len(low_score_swings)
        mistakes.append(MistakePattern(
            pattern_id="low_hunter_swing",
            description="Swing trading stocks with Hunter score < 5.5 (weak fundamentals)",
            occurrences=len(low_score_swings),
            avg_loss_pct=round(avg_loss, 4),
            common_conditions=[f"Hunter avg {sum(r.hunter_score for r in low_score_swings)/len(low_score_swings):.1f}"],
            recommendation="Even for swings, require Hunter >= 5.5. Bad fundamentals = less recovery on pullbacks.",
            severity="WARN" if len(low_score_swings) < 4 else "ALERT",
            examples=[r.ticker for r in low_score_swings[:3]],
        ))

    # ── Pattern 3: Low-confidence pattern entries ──
    low_conf = [r for r in losses if r.pattern != "none" and r.pattern_confidence < 0.55]
    if len(low_conf) >= 2:
        avg_loss = sum(r.return_pct for r in low_conf) / len(low_conf)
        mistakes.append(MistakePattern(
            pattern_id="low_confidence_pattern",
            description="Acting on chart patterns with confidence < 55%",
            occurrences=len(low_conf),
            avg_loss_pct=round(avg_loss, 4),
            common_conditions=[f"Avg confidence {sum(r.pattern_confidence for r in low_conf)/len(low_conf):.0%}"],
            recommendation="Require pattern confidence >= 0.60 before entering. Weak patterns = coin flip.",
            severity="WARN" if len(low_conf) < 4 else "ALERT",
            examples=[r.ticker for r in low_conf[:3]],
        ))

    # ── Pattern 4: Screen-based failures — single screen only ──
    single_screen = [r for r in losses if len(r.screens_matched) == 1]
    if len(single_screen) >= 3:
        avg_loss = sum(r.return_pct for r in single_screen) / len(single_screen)
        # Compare to multi-screen losses
        multi_screen = [r for r in losses if len(r.screens_matched) > 1]
        multi_avg = sum(r.return_pct for r in multi_screen) / len(multi_screen) if multi_screen else 0
        if avg_loss < multi_avg - 0.03:  # single-screen trades lose meaningfully more
            mistakes.append(MistakePattern(
                pattern_id="single_screen_entry",
                description="Most losses came from stocks matching only one screen (weak signal confluence)",
                occurrences=len(single_screen),
                avg_loss_pct=round(avg_loss, 4),
                common_conditions=["Only 1 screen matched vs 2+ for winners"],
                recommendation="Prefer stocks matching 2+ screens. Confluence = stronger signal.",
                severity="INFO",
                examples=[r.ticker for r in single_screen[:3]],
            ))

    # ── Pattern 5: Quick losses — held < 5 days then stopped out ──
    quick_stops = [r for r in losses if r.hold_days is not None and r.hold_days < 5
                   and r.exit_reason and "stop" in r.exit_reason.lower()]
    if len(quick_stops) >= 2:
        avg_loss = sum(r.return_pct for r in quick_stops) / len(quick_stops)
        mistakes.append(MistakePattern(
            pattern_id="quick_stop_outs",
            description="Stopped out within 5 days of entry — entering too early before pattern sets up",
            occurrences=len(quick_stops),
            avg_loss_pct=round(avg_loss, 4),
            common_conditions=["Avg hold before stop: <5 days"],
            recommendation="Wait for pattern to tighten (ATR compression) before entering. Patience at entry.",
            severity="WARN" if len(quick_stops) < 4 else "ALERT",
            examples=[r.ticker for r in quick_stops[:3]],
        ))

    _save_mistakes(mistakes)
    if mistakes:
        logger.info(f"[Feedback] Updated mistake patterns: {[m.pattern_id for m in mistakes]}")


# ── Swing gate exploration / self-adaptation ──────────────────────────────────
# Exploration mode enters setups that fail the strict selection gates, tags them
# with which gate they violated (stored in screens_matched as "strict:<gate>"),
# and half-sizes them. Once a violated gate accumulates enough closed trades,
# the evidence decides: if that cohort underperforms, THE GATE TIGHTENS ITSELF.
# One-way ratchet — the system only tightens; a human re-loosens by editing
# data/swing_gate_state.json (or deleting it to reset all gates to loose).

GATE_STATE_FILE = os.path.join("data", "swing_gate_state.json")
SWING_GATES = ("signals", "risk_reward", "entry_zone")
GATE_MIN_CLOSED = 10          # closed trades a cohort needs before it can be judged
GATE_KILL_HIT_RATE = 0.45     # cohort hit rate below this → tighten the gate
GATE_KILL_EDGE = -0.03        # or avg return 3pts+ worse than the clean cohort


def load_gate_state() -> dict:
    """Per-gate mode: 'loose' (exploration default) or 'strict' (self-tightened)."""
    state = {g: "loose" for g in SWING_GATES}
    try:
        if os.path.exists(GATE_STATE_FILE):
            saved = json.load(open(GATE_STATE_FILE))
            for g in SWING_GATES:
                if saved.get(g) in ("loose", "strict"):
                    state[g] = saved[g]
            state["history"] = saved.get("history", [])
    except Exception as e:
        logger.warning(f"[GateAdapt] state load failed, using defaults: {e}")
    state.setdefault("history", [])
    return state


def _save_gate_state(state: dict) -> None:
    os.makedirs("data", exist_ok=True)
    with open(GATE_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def gate_cohort_report() -> dict:
    """
    Scoreboard: closed swing trades grouped by which strict gate they violated,
    vs the 'clean' cohort (violated none). This is what adapt_swing_gates judges.
    """
    records = _load_records()
    closed = [r for r in records
              if r.signal_type == "SWING" and r.outcome in ("WIN", "LOSS", "SCRATCH")
              and r.return_pct is not None]

    def stats(recs):
        if not recs:
            return {"closed": 0, "wins": 0, "hit_rate": None, "avg_return_pct": None}
        wins = [r for r in recs if r.outcome == "WIN"]
        return {
            "closed": len(recs),
            "wins": len(wins),
            "hit_rate": round(len(wins) / len(recs), 3),
            "avg_return_pct": round(sum(r.return_pct for r in recs) / len(recs), 4),
        }

    report = {"clean": stats([r for r in closed
                              if not any(t.startswith("strict:") for t in r.screens_matched)])}
    for gate in SWING_GATES:
        tag = f"strict:{gate}"
        report[gate] = stats([r for r in closed if tag in r.screens_matched])
    return report


def adapt_swing_gates() -> list[str]:
    """
    The self-adaptation step — called before every swing auto-entry pass.
    For each still-loose gate with >= GATE_MIN_CLOSED closed trades in its
    violation cohort: tighten it if the cohort's hit rate is below
    GATE_KILL_HIT_RATE or its avg return trails the clean cohort by GATE_KILL_EDGE.
    Returns human-readable action strings (empty when no gate changed).
    """
    state = load_gate_state()
    report = gate_cohort_report()
    clean_avg = report["clean"]["avg_return_pct"]
    actions: list[str] = []

    for gate in SWING_GATES:
        if state.get(gate) != "loose":
            continue
        c = report[gate]
        if c["closed"] < GATE_MIN_CLOSED:
            continue
        bad_hit = c["hit_rate"] is not None and c["hit_rate"] < GATE_KILL_HIT_RATE
        bad_edge = (clean_avg is not None and c["avg_return_pct"] is not None
                    and c["avg_return_pct"] < clean_avg + GATE_KILL_EDGE)
        if bad_hit or bad_edge:
            state[gate] = "strict"
            evidence = (
                f"{c['closed']} closed, hit rate {c['hit_rate']:.0%}, "
                f"avg {c['avg_return_pct']:+.1%}"
                + (f" vs clean {clean_avg:+.1%}" if clean_avg is not None else "")
            )
            state["history"].append({
                "date": date.today().isoformat(),
                "gate": gate,
                "action": "TIGHTENED",
                "evidence": evidence,
            })
            actions.append(f"Gate '{gate}' SELF-TIGHTENED — {evidence}")
            logger.info(f"[GateAdapt] {actions[-1]}")

    if actions:
        _save_gate_state(state)
    return actions


# ── Mistake-pattern conviction penalty ────────────────────────────────────────
# Mirrors the macro-pulse penalty design: small, bounded, evidence-gated.
# A pattern only penalizes a candidate when (a) it has enough closed-loss
# occurrences behind it and (b) the candidate ACTUALLY matches the pattern's
# entry conditions — a generic "we lost money recently" must never bleed into
# unrelated setups (that would codify recency bias, see bias_check.py).

MISTAKE_MIN_OCCURRENCES = 3          # detector fires at 2; penalizing needs more evidence
MISTAKE_PENALTY = {"ALERT": 1.0, "WARN": 0.5, "INFO": 0.0}   # INFO = note only
MISTAKE_PENALTY_CAP = 1.5            # same ceiling as the macro penalty


def mistake_conviction_penalty(
    hunter_score: float,
    signal_type: str = "BUY",
    rsi: Optional[float] = None,
    pattern: Optional[str] = None,
    pattern_confidence: Optional[float] = None,
    n_screens: Optional[int] = None,
) -> tuple[float, list[str]]:
    """
    Match a candidate's entry conditions against learned mistake patterns.
    Returns (penalty 0..MISTAKE_PENALTY_CAP, human-readable reasons).
    Unknown inputs (None / 0.0 hunter) never match — no evidence, no penalty.
    """
    penalty = 0.0
    reasons: list[str] = []

    for m in _load_mistakes():
        if m.occurrences < MISTAKE_MIN_OCCURRENCES:
            continue

        matched = False
        if m.pattern_id == "high_rsi_entry":
            matched = rsi is not None and rsi > 68
        elif m.pattern_id == "low_hunter_swing":
            matched = (signal_type in ("SWING", "MOMENTUM")
                       and 0 < hunter_score < 5.5)
        elif m.pattern_id == "low_confidence_pattern":
            matched = (pattern is not None and pattern != "none"
                       and pattern_confidence is not None
                       and 0 < pattern_confidence < 0.55)
        elif m.pattern_id == "single_screen_entry":
            matched = n_screens == 1
        # quick_stop_outs has no candidate-observable condition at signal time

        if not matched:
            continue

        p = MISTAKE_PENALTY.get(m.severity, 0.0)
        penalty += p
        reasons.append(
            f"Matches past mistake '{m.description}' "
            f"({m.occurrences} losses, avg {m.avg_loss_pct:+.1%})"
            + (f" — conviction -{p:.1f}" if p else " — noted")
        )

    return min(penalty, MISTAKE_PENALTY_CAP), reasons


def mistake_report() -> list[MistakePattern]:
    """Load current mistake patterns, sorted by severity then occurrences."""
    mistakes = _load_mistakes()
    sev_order = {"ALERT": 0, "WARN": 1, "INFO": 2}
    mistakes.sort(key=lambda m: (sev_order.get(m.severity, 3), -m.occurrences))
    return mistakes


# ── Weekly summary ────────────────────────────────────────────────────────────

def weekly_feedback_summary() -> dict:
    """
    Full feedback summary for the weekly review dashboard.
    Returns screen stats, mistake patterns, and overall system health.
    """
    records = _load_records()
    closed = [r for r in records if r.outcome in ("WIN", "LOSS", "SCRATCH")]
    open_signals = [r for r in records if r.outcome == "OPEN"]

    screens = screen_report(min_trades=3)
    mistakes = mistake_report()

    # Kill list — screens performing below threshold
    kill_list = [s.screen_name for s in screens if s.status == "KILL"]
    watch_list = [s.screen_name for s in screens if s.status == "WATCH"]

    # Overall system hit rate
    wins = [r for r in closed if r.outcome == "WIN"]
    overall_hit_rate = len(wins) / len(closed) if closed else None

    # Best and worst screens
    best = screens[0] if screens else None
    worst = screens[-1] if screens else None

    # Recent momentum — last 10 vs prior 10
    recent = sorted(closed, key=lambda r: r.signal_date or "")[-10:]
    prior = sorted(closed, key=lambda r: r.signal_date or "")[-20:-10]
    recent_hr = len([r for r in recent if r.outcome == "WIN"]) / len(recent) if recent else None
    prior_hr = len([r for r in prior if r.outcome == "WIN"]) / len(prior) if prior else None
    improving = (recent_hr > prior_hr) if (recent_hr and prior_hr) else None

    return {
        "total_signals": len(records),
        "open_signals": len(open_signals),
        "closed_signals": len(closed),
        "overall_hit_rate": round(overall_hit_rate, 3) if overall_hit_rate else None,
        "recent_hit_rate": round(recent_hr, 3) if recent_hr else None,
        "trend": "IMPROVING" if improving else ("DECLINING" if improving is False else "UNKNOWN"),
        "screen_stats": [asdict(s) for s in screens],
        "kill_list": kill_list,
        "watch_list": watch_list,
        "best_screen": asdict(best) if best else None,
        "worst_screen": asdict(worst) if worst else None,
        "mistake_patterns": [asdict(m) for m in mistakes],
        "alert_count": len([m for m in mistakes if m.severity == "ALERT"]),
    }


# ── Hook into daily scan ───────────────────────────────────────────────────────

def auto_log_scan_signals(results: list) -> None:
    """
    Called from daily_scan.py after run_batch() completes.
    Logs all BUY signals as open records for outcome tracking.
    results: list of AnalysisResult from orchestrator
    """
    for r in results:
        if r.signal == "BUY":
            log_signal(
                ticker=r.ticker,
                screens_matched=[],        # screener screens not yet passed through here
                signal_type="BUY",
                entry_price=0.0,           # placeholder — filled when decision logged
                hunter_score=r.hunter_score,
                rsi_at_entry=0.0,          # available in price data but not in AnalysisResult yet
                pattern="none",
                pattern_confidence=0.0,
            )
