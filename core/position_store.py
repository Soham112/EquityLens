"""
Position Store — persistence for three distinct trading tracks.

  SWING      — technical pattern entry, 2–6 week hold, hard profit target
  MOMENTUM   — promoted swing, trailing stop, no cap (target can be 50%+)
  LONG_TERM  — fundamentals-driven, DCA monthly, never sold on signal
  DCA        — scheduled add to an existing long-term conviction hold

Files:
  data/swing_positions.json     — SWING and MOMENTUM positions
  data/longterm_positions.json  — LONG_TERM positions
  data/dca_schedule.json        — DCA recurring add schedules
"""
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = "data"

# Single source of truth for total paper portfolio capital.
# All three tracks (Swing, Long-Term, Growth/Speculative) draw from this pool.
UNIFIED_CAPITAL = 5000.0
SWING_FILE = os.path.join(DATA_DIR, "swing_positions.json")
LONGTERM_FILE = os.path.join(DATA_DIR, "longterm_positions.json")
DCA_FILE = os.path.join(DATA_DIR, "dca_schedule.json")


@dataclass
class SwingPosition:
    ticker: str
    sector: str
    track: str                        # "SWING" or "MOMENTUM"
    entry_price: float
    entry_date: str                   # ISO date
    shares: float
    invested_dollars: float
    pattern: str
    pattern_confidence: float
    price_structure: str
    screens_matched: list[str]
    exit_plan: dict                   # serialized ExitPlan
    pattern_thesis: str
    peak_price: float                 # highest price since entry (for trailing stop)
    current_price: float              # updated on load
    stop_price: float                 # calculated from exit_plan
    target_price: Optional[float]     # None for MOMENTUM (no hard cap)
    days_held: int = 0
    promotion_eligible: bool = False
    status: str = "OPEN"             # "OPEN" | "EXITED" | "PROMOTED"
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None


@dataclass
class LongTermPosition:
    ticker: str
    sector: str
    entry_price: float                # first purchase price
    entry_date: str
    total_shares: float               # grows with DCA adds
    total_invested: float             # grows with each DCA add
    avg_cost_basis: float             # total_invested / total_shares
    current_price: float
    hunter_score_at_entry: float
    thesis: str                       # why we own this
    dca_amount: Optional[float]       # monthly add amount ($)
    dca_day_of_month: int = 1        # day to add each month
    next_dca_date: Optional[str] = None
    status: str = "OPEN"            # "OPEN" | "CLOSED"
    adds: list = field(default_factory=list)   # list of {date, price, shares, dollars}


@dataclass
class DCASchedule:
    ticker: str
    monthly_amount: float
    day_of_month: int                # 1–28
    next_date: str                   # ISO date of next add
    total_added: float = 0.0
    add_count: int = 0


# ── IO helpers ─────────────────────────────────────────────────────────────────

def _load(path: str) -> list:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[PositionStore] Load failed {path}: {e}")
        return []


def _save(path: str, data: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Swing / Momentum positions ──────────────────────────────────────────────

def load_swing_positions(status: str = "OPEN") -> list[SwingPosition]:
    raw = _load(SWING_FILE)
    positions = [SwingPosition(**r) for r in raw]
    if status:
        positions = [p for p in positions if p.status == status]
    # Refresh days_held
    today = date.today()
    for p in positions:
        try:
            entry = date.fromisoformat(p.entry_date)
            p.days_held = (today - entry).days
        except Exception:
            pass
    return positions


def save_swing_position(pos: SwingPosition) -> None:
    all_raw = _load(SWING_FILE)
    existing = [r for r in all_raw if r["ticker"] != pos.ticker or r["status"] != "OPEN"]
    existing.append(asdict(pos))
    _save(SWING_FILE, existing)
    logger.info(f"[PositionStore] Saved swing position: {pos.ticker} [{pos.track}]")


def update_swing_position(ticker: str, **kwargs) -> bool:
    """Update fields on an open swing position."""
    all_raw = _load(SWING_FILE)
    updated = False
    for r in all_raw:
        if r["ticker"] == ticker and r["status"] == "OPEN":
            r.update(kwargs)
            updated = True
    if updated:
        _save(SWING_FILE, all_raw)
    return updated


def exit_swing_position(ticker: str, exit_price: float, reason: str) -> bool:
    all_raw = _load(SWING_FILE)
    found = False
    for r in all_raw:
        if r["ticker"] == ticker and r["status"] == "OPEN":
            r["status"] = "EXITED"
            r["exit_date"] = date.today().isoformat()
            r["exit_price"] = exit_price
            r["exit_reason"] = reason
            found = True
    if found:
        _save(SWING_FILE, all_raw)
        logger.info(f"[PositionStore] Exited swing position: {ticker} @ ${exit_price} — {reason}")
    return found


def promote_swing_position(ticker: str, new_track: str, new_exit_plan: dict) -> bool:
    """Promote SWING → MOMENTUM or SWING/MOMENTUM → LONG_TERM."""
    all_raw = _load(SWING_FILE)
    found = False
    for r in all_raw:
        if r["ticker"] == ticker and r["status"] == "OPEN":
            r["track"] = new_track
            r["exit_plan"] = new_exit_plan
            r["status"] = "PROMOTED" if new_track == "LONG_TERM" else "OPEN"
            found = True
    if found:
        _save(SWING_FILE, all_raw)
        logger.info(f"[PositionStore] Promoted {ticker} → {new_track}")
    return found


# ── Long-term positions ────────────────────────────────────────────────────

def load_longterm_positions(status: str = "OPEN") -> list[LongTermPosition]:
    raw = _load(LONGTERM_FILE)
    positions = [LongTermPosition(**r) for r in raw]
    if status:
        positions = [p for p in positions if p.status == status]
    return positions


def save_longterm_position(pos: LongTermPosition) -> None:
    all_raw = _load(LONGTERM_FILE)
    existing = [r for r in all_raw if r["ticker"] != pos.ticker or r["status"] != "OPEN"]
    existing.append(asdict(pos))
    _save(LONGTERM_FILE, existing)
    logger.info(f"[PositionStore] Saved long-term position: {pos.ticker}")


def add_dca_to_longterm(ticker: str, price: float, dollars: float) -> bool:
    """Record a DCA add to an existing long-term position."""
    all_raw = _load(LONGTERM_FILE)
    found = False
    for r in all_raw:
        if r["ticker"] == ticker and r["status"] == "OPEN":
            shares_added = dollars / price
            r["total_shares"] = round(r["total_shares"] + shares_added, 6)
            r["total_invested"] = round(r["total_invested"] + dollars, 2)
            r["avg_cost_basis"] = round(r["total_invested"] / r["total_shares"], 4)
            r["adds"] = r.get("adds", []) + [{
                "date": date.today().isoformat(),
                "price": price,
                "shares": round(shares_added, 6),
                "dollars": dollars,
            }]
            # Advance next DCA date by ~1 month
            try:
                next_d = date.fromisoformat(r["next_dca_date"])
                if next_d.month == 12:
                    r["next_dca_date"] = date(next_d.year + 1, 1, next_d.day).isoformat()
                else:
                    r["next_dca_date"] = date(next_d.year, next_d.month + 1, next_d.day).isoformat()
            except Exception:
                pass
            found = True
    if found:
        _save(LONGTERM_FILE, all_raw)
        logger.info(f"[PositionStore] DCA add: {ticker} ${dollars} @ ${price}")
    return found


# ── DCA schedule ────────────────────────────────────────────────────────────

def load_dca_schedules() -> list[DCASchedule]:
    raw = _load(DCA_FILE)
    return [DCASchedule(**r) for r in raw]


def save_dca_schedule(sched: DCASchedule) -> None:
    all_raw = _load(DCA_FILE)
    existing = [r for r in all_raw if r["ticker"] != sched.ticker]
    existing.append(asdict(sched))
    _save(DCA_FILE, existing)
    logger.info(f"[PositionStore] Saved DCA schedule: {sched.ticker} ${sched.monthly_amount}/month")


def get_due_dca_schedules() -> list[DCASchedule]:
    """Return schedules where next_date is today or earlier."""
    today = date.today().isoformat()
    return [s for s in load_dca_schedules() if s.next_date <= today]


# ── Capital overview ─────────────────────────────────────────────────────────

def capital_overview(starting_capital: float = UNIFIED_CAPITAL) -> dict:
    """
    Unified capital view across ALL tracks and pools.

    Sources:
      - Swing/Long-Term positions (position_store)
      - Main paper portfolio (paper_trading.py) — maps to Long-Term track
      - Growth Scout portfolio (growth_paper_trading.py) — maps to Swing/Speculative track

    Targets: 30% Swing (incl. speculative) / 60% Long-Term / 10% Cash
    """
    swing_positions = load_swing_positions("OPEN")
    longterm_positions = load_longterm_positions("OPEN")

    # Capital deployed via position_store
    ps_swing = sum(p.invested_dollars for p in swing_positions)
    ps_lt = sum(p.total_invested for p in longterm_positions)

    # Capital deployed via legacy paper portfolios
    paper_lt = 0.0
    paper_swing_speculative = 0.0
    paper_position_count = 0
    growth_position_count = 0

    try:
        from core.paper_trading import load_paper_portfolio
        pp = load_paper_portfolio()
        # Main paper portfolio → Long-Term track (BUY signals from orchestrator)
        paper_lt = sum(pos.cost_basis for pos in pp.positions.values())
        paper_position_count = len(pp.positions)
    except Exception as e:
        logger.debug(f"[CapitalOverview] paper_trading not available: {e}")

    try:
        from core.growth_paper_trading import load_growth_portfolio
        gp = load_growth_portfolio()
        # Growth Scout → Swing/Speculative track
        paper_swing_speculative = sum(
            pos.entry_price * pos.shares for pos in gp.positions.values()
        )
        growth_position_count = len(gp.positions)
    except Exception as e:
        logger.debug(f"[CapitalOverview] growth_paper_trading not available: {e}")

    swing_invested = ps_swing + paper_swing_speculative
    lt_invested = ps_lt + paper_lt
    total_invested = swing_invested + lt_invested
    cash = max(0.0, starting_capital - total_invested)

    cap = starting_capital if starting_capital else 1.0

    return {
        "starting_capital": starting_capital,
        # Swing (30% target — includes speculative/growth scouts)
        "swing_invested": round(swing_invested, 2),
        "swing_pct": round(swing_invested / cap, 4),
        "swing_target_pct": 0.30,
        "n_swing": len(swing_positions) + growth_position_count,
        # Long-Term (60% target — includes paper portfolio BUY signals)
        "longterm_invested": round(lt_invested, 2),
        "longterm_pct": round(lt_invested / cap, 4),
        "longterm_target_pct": 0.60,
        "n_longterm": len(longterm_positions) + paper_position_count,
        # Cash / dry powder (15% floor recommended)
        "cash": round(cash, 2),
        "cash_pct": round(cash / cap, 4),
        "cash_target_pct": 0.10,
        # Total deployed
        "total_invested": round(total_invested, 2),
        "total_invested_pct": round(total_invested / cap, 4),
    }


def list_closed_trades() -> list[dict]:
    """
    Unified realized-trade history across both paper portfolios, newest first.

    Sources:
      - paper_trades.jsonl (long-term track) — SELL rows carry no entry price,
        so avg cost is reconstructed by replaying BUYs chronologically
      - growth_trades.json (swing track) — SELL/TRIM rows carry pnl directly

    Each row: {date, ticker, track, action, shares, exit_price, entry_price,
               pnl, pnl_pct, reason}
    """
    rows: list[dict] = []

    try:
        from core.paper_trading import load_trade_history
        avg_cost: dict[str, float] = {}
        held: dict[str, float] = {}
        for t in load_trade_history():
            if t.action == "BUY":
                prev_sh = held.get(t.ticker, 0.0)
                new_sh = prev_sh + t.shares
                avg_cost[t.ticker] = (
                    (avg_cost.get(t.ticker, 0.0) * prev_sh + t.price * t.shares) / new_sh
                    if new_sh else t.price
                )
                held[t.ticker] = new_sh
            elif t.action.startswith("SELL"):
                entry = avg_cost.get(t.ticker)
                held[t.ticker] = max(0.0, held.get(t.ticker, 0.0) - t.shares)
                pnl = (t.price - entry) * t.shares if entry else None
                rows.append({
                    "date": t.date[:10],
                    "ticker": t.ticker,
                    "track": "LT",
                    "action": t.action,
                    "shares": t.shares,
                    "exit_price": t.price,
                    "entry_price": round(entry, 4) if entry else None,
                    "pnl": round(pnl, 2) if pnl is not None else None,
                    "pnl_pct": round((t.price - entry) / entry, 4) if entry else None,
                    "reason": t.reason,
                })
    except Exception as e:
        logger.warning(f"list_closed_trades: paper trade history failed: {e}")

    try:
        from core.growth_paper_trading import load_growth_trades
        for t in load_growth_trades():
            if t.get("action") == "BUY":
                continue
            shares = t.get("shares") or 0.0
            price = t.get("price") or 0.0
            pnl = t.get("pnl")
            entry = round(price - pnl / shares, 4) if (pnl is not None and shares) else None
            rows.append({
                "date": str(t.get("date", ""))[:10],
                "ticker": t.get("ticker"),
                "track": "SWING",
                "action": t.get("action"),
                "shares": shares,
                "exit_price": price,
                "entry_price": entry,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round((price - entry) / entry, 4) if entry else None,
                "reason": t.get("reason", ""),
            })
    except Exception as e:
        logger.warning(f"list_closed_trades: growth trade history failed: {e}")

    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows
