"""
Decision Capture Workflow

Runs after market close (4:30 PM). Reads today's BUY signals, checks which
ones don't have a journal entry yet, and writes data/pending_decisions.json.

The dashboard surfaces these as "Did you invest?" prompts.
You can also call log_decision() directly to record a decision.
"""
import datetime
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.journal import TradeRecord, log_decision, load_records
from core.persistence import load_portfolio, add_position, PortfolioPosition

logger = logging.getLogger(__name__)

PENDING_FILE = os.path.join("data", "pending_decisions.json")


def get_todays_buy_signals() -> list[dict]:
    """Load today's scan and return BUY-signal results."""
    date_str = datetime.date.today().isoformat()
    for prefix in ("scan_", "daily_scan_"):
        path = Path("data") / f"{prefix}{date_str}.json"
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            return [r for r in data.get("results", []) if r.get("signal") == "BUY"]
    return []


def get_already_journaled_today() -> set[str]:
    """Tickers that already have a journal entry from today."""
    today = datetime.date.today().isoformat()
    records = load_records(days_back=1)
    return {r.ticker for r in records if r.recorded_at and r.recorded_at[:10] == today}


def build_pending_decisions() -> list[dict]:
    """
    Returns list of BUY signals from today that haven't been journaled yet.
    Each entry is a dict the dashboard can render as a decision card.
    """
    buy_signals = get_todays_buy_signals()
    journaled = get_already_journaled_today()

    pending = []
    for r in buy_signals:
        ticker = r.get("ticker", "")
        if ticker not in journaled:
            pending.append({
                "ticker": ticker,
                "conviction": r.get("conviction"),
                "hunter_score": r.get("hunter_score"),
                "thesis": r.get("thesis", ""),
                "stop_tier1": r.get("stop_tier1"),
                "stop_tier2": r.get("stop_tier2"),
                "stop_tier3": r.get("stop_tier3"),
                "recommended_pct": r.get("recommended_pct"),
                "alerts": r.get("alerts", []),
                "signal_date": datetime.date.today().isoformat(),
                "status": "PENDING",  # PENDING | INVESTED | SKIPPED
            })

    # Persist so dashboard can render them
    os.makedirs("data", exist_ok=True)
    existing = _load_pending()
    # Merge: keep prior pending entries that haven't been resolved
    existing_tickers = {e["ticker"] for e in existing if e.get("status") == "PENDING"}
    for p in pending:
        if p["ticker"] not in existing_tickers:
            existing.append(p)

    with open(PENDING_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    logger.info(f"Pending decisions: {len(pending)} new, {len(existing)} total")
    return pending


def _load_pending() -> list[dict]:
    if not os.path.exists(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def record_investment(
    ticker: str,
    entry_price: float,
    shares: float,
    position_pct: float,
    notes: str = "",
) -> None:
    """
    Call this when you decide to invest after a BUY signal.
    Logs to journal + adds to portfolio tracker.
    """
    # Load the signal details from pending
    pending = _load_pending()
    signal = next((p for p in pending if p["ticker"] == ticker and p["status"] == "PENDING"), {})

    record = TradeRecord(
        ticker=ticker,
        recorded_at=datetime.datetime.now().isoformat(),
        model_conviction=signal.get("conviction", 0),
        model_signal="BUY",
        your_decision="BUY",
        decision_reason=notes or "Invested per BUY signal",
        entry_price=entry_price,
        entry_date=datetime.date.today().isoformat(),
        stop_tier1=signal.get("stop_tier1"),
        stop_tier2=signal.get("stop_tier2"),
        stop_tier3=signal.get("stop_tier3"),
        exit_price=None,
        exit_date=None,
        exit_trigger=None,
        return_pct=None,
        thesis_outcome=None,
        error_type=None,
        model_lesson=None,
    )
    log_decision(record)

    # Add to portfolio persistence
    add_position(PortfolioPosition(
        ticker=ticker,
        entry_date=datetime.date.today().isoformat(),
        entry_price=entry_price,
        current_price=entry_price,
        shares=shares,
        position_pct=position_pct,
        stop_tier1=signal.get("stop_tier1"),
        stop_tier2=signal.get("stop_tier2"),
        stop_tier3=signal.get("stop_tier3"),
        last_conviction=signal.get("conviction", 0),
        notes=notes,
    ))

    # Mark resolved in pending
    _resolve_pending(ticker, "INVESTED")
    logger.info(f"Recorded investment: {ticker} @ ${entry_price:.2f} x {shares} shares")


def record_skip(ticker: str, reason: str = "") -> None:
    """Call this when you decide NOT to invest despite a BUY signal."""
    pending = _load_pending()
    signal = next((p for p in pending if p["ticker"] == ticker and p["status"] == "PENDING"), {})

    record = TradeRecord(
        ticker=ticker,
        recorded_at=datetime.datetime.now().isoformat(),
        model_conviction=signal.get("conviction", 0),
        model_signal="BUY",
        your_decision="SKIP",
        decision_reason=reason or "Skipped",
        entry_price=None,
        entry_date=None,
        stop_tier1=None, stop_tier2=None, stop_tier3=None,
        exit_price=None, exit_date=None, exit_trigger=None,
        return_pct=None, thesis_outcome=None,
        error_type=None, model_lesson=None,
    )
    log_decision(record)
    _resolve_pending(ticker, "SKIPPED")
    logger.info(f"Recorded skip: {ticker} — {reason}")


def _resolve_pending(ticker: str, status: str) -> None:
    pending = _load_pending()
    for p in pending:
        if p["ticker"] == ticker and p["status"] == "PENDING":
            p["status"] = status
            p["resolved_at"] = datetime.datetime.now().isoformat()
    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pending = build_pending_decisions()
    if pending:
        print(f"\n{'='*60}")
        print(f"PENDING DECISIONS — {datetime.date.today()}")
        print(f"{'='*60}")
        for p in pending:
            print(f"\n  {p['ticker']:6} | Conviction {p['conviction']:.1f} | "
                  f"Rec size {p.get('recommended_pct', 0):.1%}")
            print(f"  Thesis: {p['thesis'][:80]}")
            if p.get("alerts"):
                for a in p["alerts"][:2]:
                    print(f"  ⚠  {a}")
        print(f"\nLog your decision:")
        print(f"  from workflows.decision_capture import record_investment, record_skip")
        print(f"  record_investment('TICKER', entry_price=..., shares=..., position_pct=...)")
        print(f"  record_skip('TICKER', reason='...')")
    else:
        print("No pending decisions — all BUY signals for today are journaled.")
