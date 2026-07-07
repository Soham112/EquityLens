"""
Trade-level statistics from the paper trade logs.

Signal hit rates measure whether the SIGNALS were right; these measure whether
the PORTFOLIO makes money — win rate, expectancy, profit factor, max drawdown.
These are the numbers that answer "is this system ready for real capital."

Round-trip accounting: buys accumulate a per-ticker cost basis; every sell
realizes P&L against the average cost. A trade is "closed" when shares reach
zero — trims along the way roll into that round-trip's total.
"""
import datetime
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ClosedTrade:
    ticker: str
    entry_date: str
    exit_date: str
    realized_pnl: float      # dollars, all partial sells included
    invested: float          # total cost basis of the round-trip
    return_pct: float        # realized_pnl / invested
    exit_reason: str         # reason string from the final sell


@dataclass
class _OpenLot:
    shares: float = 0.0
    cost: float = 0.0
    entry_date: str = ""
    realized: float = 0.0    # realized P&L accumulated by trims
    invested: float = 0.0    # total dollars ever put in this round-trip


def _close_trades_from_events(events: list[dict]) -> list[ClosedTrade]:
    """
    events: chronological list of {date, ticker, action, shares, price, reason}
    where action is BUY or any SELL_*/TRIM_*/SELL variant.
    """
    open_lots: dict[str, _OpenLot] = {}
    closed: list[ClosedTrade] = []

    for e in events:
        t = e["ticker"]
        shares = float(e.get("shares", 0) or 0)
        price = float(e.get("price", 0) or 0)
        if shares <= 0 or price <= 0:
            continue

        if e["action"] == "BUY":
            lot = open_lots.setdefault(t, _OpenLot(entry_date=e.get("date", "")))
            if lot.shares <= 0:
                lot.entry_date = e.get("date", "")
            lot.shares += shares
            lot.cost += shares * price
            lot.invested += shares * price
        else:  # any sell/trim
            lot = open_lots.get(t)
            if not lot or lot.shares <= 0:
                continue  # sell without tracked buy (pre-log history)
            shares = min(shares, lot.shares)
            avg_cost = lot.cost / lot.shares
            lot.realized += shares * (price - avg_cost)
            lot.cost -= shares * avg_cost
            lot.shares -= shares
            if lot.shares <= 1e-6:  # round-trip complete
                closed.append(ClosedTrade(
                    ticker=t,
                    entry_date=lot.entry_date,
                    exit_date=e.get("date", ""),
                    realized_pnl=round(lot.realized, 2),
                    invested=round(lot.invested, 2),
                    return_pct=round(lot.realized / lot.invested, 4) if lot.invested else 0.0,
                    exit_reason=e.get("reason", ""),
                ))
                del open_lots[t]

    return closed


def _lt_events() -> list[dict]:
    path = Path("data/paper_trades.jsonl")
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
                events.append({
                    "date": d.get("date", ""), "ticker": d["ticker"],
                    "action": "BUY" if d["action"] == "BUY" else "SELL",
                    "shares": d.get("shares", 0), "price": d.get("price", 0),
                    "reason": d.get("reason", ""),
                })
            except Exception:
                continue
    return events


def _growth_events() -> list[dict]:
    path = Path("data/growth_trades.json")
    if not path.exists():
        return []
    try:
        trades = json.loads(path.read_text())
    except Exception:
        return []
    return [{
        "date": (d.get("date") or "")[:10], "ticker": d["ticker"],
        "action": "BUY" if d.get("action") == "BUY" else "SELL",
        "shares": d.get("shares", 0), "price": d.get("price", 0),
        "reason": d.get("reason", ""),
    } for d in trades]


def _max_drawdown(values: list[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    peak = values[0]
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, (v - peak) / peak)
    return round(max_dd, 4)


def _stats_for(closed: list[ClosedTrade]) -> dict:
    if not closed:
        return {"closed_trades": 0, "note": "no completed round-trips yet"}
    wins = [c for c in closed if c.realized_pnl > 0]
    losses = [c for c in closed if c.realized_pnl <= 0]
    gross_win = sum(c.realized_pnl for c in wins)
    gross_loss = abs(sum(c.realized_pnl for c in losses))
    return {
        "closed_trades": len(closed),
        "win_rate": round(len(wins) / len(closed), 3),
        "avg_win_pct": round(sum(c.return_pct for c in wins) / len(wins), 4) if wins else None,
        "avg_loss_pct": round(sum(c.return_pct for c in losses) / len(losses), 4) if losses else None,
        "expectancy_pct": round(sum(c.return_pct for c in closed) / len(closed), 4),
        "expectancy_dollars": round(sum(c.realized_pnl for c in closed) / len(closed), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_realized_pnl": round(sum(c.realized_pnl for c in closed), 2),
        "trades": [vars(c) for c in closed],
    }


def compute_trade_stats() -> dict:
    """Full trade-level report across both paper portfolios."""
    lt_closed = _close_trades_from_events(_lt_events())
    growth_closed = _close_trades_from_events(_growth_events())

    # Max drawdown from the LT P&L snapshots (growth has no history file yet)
    lt_dd = None
    try:
        history = json.loads(Path("data/paper_pnl_history.json").read_text())
        lt_dd = _max_drawdown([h["total_value"] for h in history])
    except Exception:
        pass

    combined = _stats_for(lt_closed + growth_closed)
    combined.pop("trades", None)  # keep the roll-up light
    return {
        "as_of": datetime.date.today().isoformat(),
        "long_term": _stats_for(lt_closed),
        "swing": _stats_for(growth_closed),
        "combined": combined,
        "lt_max_drawdown": lt_dd,
    }


def format_trade_stats(stats: dict) -> list[str]:
    """Human-readable lines for reports."""
    lines = ["Trade-Level Stats (closed round-trips):"]
    for label, key in (("Long-term", "long_term"), ("Swing", "swing"), ("Combined", "combined")):
        s = stats.get(key, {})
        if not s.get("closed_trades"):
            lines.append(f"  {label:10} — no closed trades yet")
            continue
        pf = s.get("profit_factor")
        lines.append(
            f"  {label:10} — {s['closed_trades']} closed | win rate {s['win_rate']:.0%} | "
            f"expectancy {s['expectancy_pct']:+.1%}/trade (${s['expectancy_dollars']:+.2f}) | "
            f"profit factor {pf if pf is not None else 'inf'} | "
            f"realized ${s['total_realized_pnl']:+.2f}"
        )
    if stats.get("lt_max_drawdown") is not None:
        lines.append(f"  LT max drawdown: {stats['lt_max_drawdown']:.1%}")
    return lines
