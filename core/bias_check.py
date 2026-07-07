"""
Behavioral Bias Checkpoints [GAP 17]

Detects five cognitive biases using journal history + scan data.
Called at two points:
  1. Pre-decision (before acting on a BUY signal) — analyze_pre_decision()
  2. End-of-scan  (daily batch)                   — scan_for_biases()

Biases detected:
  RECENCY_BIAS      Recent losses → undersizing winners; recent wins → oversizing
  FOMO              Entering after large run-up (stock up >15% in 20d before signal)
  LOSS_AVERSION     Cutting winners early; holding losers past stop; payoff ratio < 1
  OVERCONFIDENCE    Conviction clustering at 9-10 after a winning streak
  ANCHORING         Systematic preference for round-number entry prices
"""
import datetime
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Thresholds ────────────────────────────────────────────────────────────────

FOMO_RUN_UP_PCT = 0.15          # 15% gain in 20d before signal → FOMO flag
LOSS_AVERSION_PAYOFF_MIN = 1.0  # payoff ratio below this triggers flag
OVERCONFIDENCE_CONVICTION_MIN = 9.0   # avg conviction after winning streak
OVERCONFIDENCE_WIN_STREAK = 4   # consecutive wins before overconfidence check
ANCHORING_ROUND_THRESHOLD = 0.05  # entry within 5% of a round number ($10, $50, $100…)
RECENCY_WINDOW = 5              # look at last N closed trades for recency bias


# ── Output types ─────────────────────────────────────────────────────────────

@dataclass
class BiasFlag:
    bias: str           # RECENCY_BIAS | FOMO | LOSS_AVERSION | OVERCONFIDENCE | ANCHORING
    severity: str       # "INFO" | "WARN" | "ALERT"
    ticker: Optional[str]
    message: str
    recommendation: str


@dataclass
class BiasReport:
    generated_at: str
    flags: list[BiasFlag]
    clean: bool         # True if no WARN/ALERT flags

    def summary(self) -> str:
        if self.clean:
            return "No behavioral bias flags. Decision environment looks clean."
        lines = [f"Behavioral Bias Report — {self.generated_at}"]
        for f in self.flags:
            lines.append(f"  [{f.severity}] {f.bias}: {f.message}")
            lines.append(f"         → {f.recommendation}")
        return "\n".join(lines)

    def has_alerts(self) -> bool:
        return any(f.severity == "ALERT" for f in self.flags)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "clean": self.clean,
            "flags": [
                {"bias": f.bias, "severity": f.severity, "ticker": f.ticker,
                 "message": f.message, "recommendation": f.recommendation}
                for f in self.flags
            ],
        }


# ── Individual bias detectors ─────────────────────────────────────────────────

def _check_recency_bias(records) -> list[BiasFlag]:
    """
    Pattern: last N closed trades are all losses → next position sized too small
    or last N are all wins → next position sized too large (overexposure).
    We detect the pattern from journal records alone (no position size history
    needed — we flag the streak so the user is aware before acting).
    """
    flags: list[BiasFlag] = []
    closed = [r for r in records if r.return_pct is not None]
    if len(closed) < RECENCY_WINDOW:
        return flags

    recent = closed[-RECENCY_WINDOW:]
    recent_rets = [r.return_pct for r in recent]
    all_losses = all(r < 0 for r in recent_rets)
    all_wins = all(r > 0 for r in recent_rets)

    if all_losses:
        avg_loss = sum(recent_rets) / len(recent_rets)
        flags.append(BiasFlag(
            bias="RECENCY_BIAS",
            severity="WARN",
            ticker=None,
            message=(
                f"Last {RECENCY_WINDOW} closed trades all losses "
                f"(avg {avg_loss:.1%}). Risk of undersizing next winner."
            ),
            recommendation=(
                "Use the model's recommended position size — don't cut it "
                "below the formula just because recent trades lost."
            ),
        ))
    elif all_wins:
        avg_win = sum(recent_rets) / len(recent_rets)
        flags.append(BiasFlag(
            bias="RECENCY_BIAS",
            severity="INFO",
            ticker=None,
            message=(
                f"Last {RECENCY_WINDOW} closed trades all wins (avg {avg_win:.1%}). "
                "Risk of oversizing next position."
            ),
            recommendation=(
                "Stick to the conviction-based sizing formula. "
                "A winning streak doesn't change the math."
            ),
        ))
    return flags


def _check_fomo(ticker: str, price_20d_ago: Optional[float], current_price: float) -> list[BiasFlag]:
    """Flag if the stock is up >FOMO_RUN_UP_PCT in the last 20 days."""
    flags: list[BiasFlag] = []
    if price_20d_ago is None or price_20d_ago <= 0:
        return flags
    run_up = (current_price - price_20d_ago) / price_20d_ago
    if run_up >= FOMO_RUN_UP_PCT:
        flags.append(BiasFlag(
            bias="FOMO",
            severity="WARN" if run_up < 0.30 else "ALERT",
            ticker=ticker,
            message=(
                f"{ticker} is up {run_up:.0%} in 20 days. "
                "Entering here may be chasing momentum, not buying value."
            ),
            recommendation=(
                "Check if the thesis changed or just the price. "
                "Consider a smaller initial position and wait for a pullback entry."
            ),
        ))
    return flags


def _check_loss_aversion(records) -> list[BiasFlag]:
    """
    Payoff ratio < 1 over recent trades = cutting winners too early or holding
    losers too long. Both are loss-aversion symptoms.
    """
    flags: list[BiasFlag] = []
    closed = [r for r in records if r.return_pct is not None]
    if len(closed) < 6:
        return flags

    winners = [r.return_pct for r in closed if r.return_pct > 0]
    losers = [abs(r.return_pct) for r in closed if r.return_pct < 0]
    if not winners or not losers:
        return flags

    avg_win = sum(winners) / len(winners)
    avg_loss = sum(losers) / len(losers)
    payoff = avg_win / avg_loss

    if payoff < LOSS_AVERSION_PAYOFF_MIN:
        flags.append(BiasFlag(
            bias="LOSS_AVERSION",
            severity="WARN",
            ticker=None,
            message=(
                f"Payoff ratio {payoff:.2f}x (winners avg {avg_win:.1%}, "
                f"losers avg {avg_loss:.1%}). Losses are larger than wins."
            ),
            recommendation=(
                "Review exit triggers: are you selling winners at the first profit-taking level "
                "but letting losers run past Tier 2 stops? Let stop-loss rules run mechanically."
            ),
        ))

    # Also flag if many exits happened before stop tier2 (cutting winners early)
    early_exits = [r for r in closed if r.exit_trigger == "THESIS_BREAK" and r.return_pct and r.return_pct > 0.05]
    if len(early_exits) >= 3:
        flags.append(BiasFlag(
            bias="LOSS_AVERSION",
            severity="INFO",
            ticker=None,
            message=(
                f"{len(early_exits)} profitable positions exited via THESIS_BREAK "
                "(possible premature exit on positive trades)."
            ),
            recommendation=(
                "Re-examine whether thesis was actually broken or if you were "
                "rationalizing an exit on a winner. Let trailing stops protect gains."
            ),
        ))
    return flags


def _check_overconfidence(records) -> list[BiasFlag]:
    """
    After a winning streak, conviction scores on new entries trend toward 9-10
    (model scores, not user's — but if the user is cherry-picking only the
    highest-conviction signals post-streak, that's overconfidence).
    """
    flags: list[BiasFlag] = []
    closed = [r for r in records if r.return_pct is not None]
    if len(closed) < OVERCONFIDENCE_WIN_STREAK + 2:
        return flags

    recent = closed[-OVERCONFIDENCE_WIN_STREAK:]
    streak_wins = sum(1 for r in recent if r.return_pct > 0)

    if streak_wins < OVERCONFIDENCE_WIN_STREAK:
        return flags

    # Check if entries AFTER the streak have unusually high conviction requirements
    # (proxy: last 3 entries all have conviction >= 9.0)
    buys_after = [r for r in records
                  if r.your_decision == "BUY"
                  and r.model_conviction >= OVERCONFIDENCE_CONVICTION_MIN]
    if len(buys_after) >= 3:
        flags.append(BiasFlag(
            bias="OVERCONFIDENCE",
            severity="WARN",
            ticker=None,
            message=(
                f"{streak_wins}-trade winning streak detected. "
                f"Recent entries all at conviction ≥ {OVERCONFIDENCE_CONVICTION_MIN:.0f} — "
                "possible overconfidence raising implicit bar."
            ),
            recommendation=(
                "The model's conviction thresholds are calibrated. "
                "Don't raise the bar to 9-10 after a hot streak — "
                "you'll miss valid 8.0 BUY setups."
            ),
        ))
    return flags


def _check_anchoring(records) -> list[BiasFlag]:
    """
    Detect if entry prices cluster near round numbers ($10, $25, $50, $100, $150, $200…).
    Anchoring = systematically waiting for a round number instead of entering at signal.
    """
    flags: list[BiasFlag] = []
    round_numbers = [10, 25, 50, 75, 100, 125, 150, 175, 200, 250, 300, 400, 500, 1000]

    entries = [r for r in records if r.entry_price and r.your_decision == "BUY"]
    if len(entries) < 5:
        return flags

    anchored = 0
    for r in entries:
        p = r.entry_price
        for rn in round_numbers:
            if abs(p - rn) / rn <= ANCHORING_ROUND_THRESHOLD:
                anchored += 1
                break

    anchored_pct = anchored / len(entries)
    if anchored_pct >= 0.5:
        flags.append(BiasFlag(
            bias="ANCHORING",
            severity="INFO",
            ticker=None,
            message=(
                f"{anchored_pct:.0%} of entries ({anchored}/{len(entries)}) "
                "are within 5% of a round price number."
            ),
            recommendation=(
                "Enter when the model signals BUY, not when price hits a "
                "psychologically convenient round number. "
                "Round numbers aren't support levels."
            ),
        ))
    return flags


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_pre_decision(
    ticker: str,
    current_price: float,
    price_20d_ago: Optional[float] = None,
    journal_lookback_days: int = 90,
) -> BiasReport:
    """
    Run all bias checks before acting on a BUY signal for `ticker`.
    Fetches price_20d_ago from yfinance if not provided.
    """
    from agents.journal import load_records

    if price_20d_ago is None:
        price_20d_ago = _fetch_price_20d_ago(ticker, current_price)

    records = load_records(days_back=journal_lookback_days)
    flags: list[BiasFlag] = []

    flags += _check_fomo(ticker, price_20d_ago, current_price)
    flags += _check_recency_bias(records)
    flags += _check_loss_aversion(records)
    flags += _check_overconfidence(records)
    flags += _check_anchoring(records)

    clean = not any(f.severity in ("WARN", "ALERT") for f in flags)
    return BiasReport(
        generated_at=datetime.datetime.now().isoformat(),
        flags=flags,
        clean=clean,
    )


def scan_for_biases(journal_lookback_days: int = 90) -> BiasReport:
    """
    End-of-scan bias sweep — no specific ticker, checks journal patterns only.
    Call from daily_scan.py after results are computed.
    """
    from agents.journal import load_records

    records = load_records(days_back=journal_lookback_days)
    flags: list[BiasFlag] = []

    flags += _check_recency_bias(records)
    flags += _check_loss_aversion(records)
    flags += _check_overconfidence(records)
    flags += _check_anchoring(records)

    clean = not any(f.severity in ("WARN", "ALERT") for f in flags)
    report = BiasReport(
        generated_at=datetime.datetime.now().isoformat(),
        flags=flags,
        clean=clean,
    )

    if not clean:
        for flag in flags:
            if flag.severity in ("WARN", "ALERT"):
                logger.warning(f"BIAS [{flag.bias}]: {flag.message}")

    return report


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_price_20d_ago(ticker: str, current_price: float) -> Optional[float]:
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="25d")
        if hist.empty or len(hist) < 20:
            return None
        return float(hist["Close"].iloc[-20])
    except Exception:
        return None
