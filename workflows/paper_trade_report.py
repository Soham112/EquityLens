"""
Paper Trading Daily Report

Runs every evening after market close. Updates all position prices,
checks stops, applies profit trims, and prints a clear P&L summary.

Shows:
  - Total portfolio value vs starting $500
  - Return vs SPY over the same period (are we beating the market?)
  - Every open position: entry, current, return %, stop status
  - Actions taken today (stops hit, trims executed)
  - Cumulative trade log
"""
import datetime
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.paper_trading import (
    auto_execute_scan_signals,
    daily_update,
    get_pnl_history,
    load_paper_portfolio,
    load_trade_history,
    reset_paper_portfolio,
    STARTING_CAPITAL,
)

logger = logging.getLogger(__name__)


def print_report(summary: dict) -> None:
    portfolio = load_paper_portfolio()
    history = get_pnl_history()
    trades = load_trade_history()

    ret = summary["total_return_pct"]
    spy = summary.get("spy_return_since_start")
    alpha = summary.get("alpha")

    print("\n" + "=" * 65)
    print(f"  EQUITYLENS PAPER PORTFOLIO — {summary['date']}")
    print("=" * 65)

    # ── Overall performance ──
    start = portfolio.start_date or "?"
    days_running = 0
    if portfolio.start_date:
        try:
            days_running = (datetime.date.today() -
                            datetime.date.fromisoformat(portfolio.start_date)).days
        except Exception:
            pass

    print(f"\n  Starting capital : ${portfolio.starting_capital:>8.2f}")
    print(f"  Current value    : ${summary['portfolio_value']:>8.2f}   "
          f"({ret:+.1%} total, {ret*100/max(days_running,1):.2f}%/day avg)")
    print(f"  Cash             : ${summary['cash']:>8.2f}")
    print(f"  Invested         : ${summary['invested']:>8.2f}  "
          f"({summary['n_positions']} positions)")
    print(f"  Running since    : {start} ({days_running} days)")

    if spy is not None:
        alpha_str = f"{alpha:+.1%}" if alpha is not None else "n/a"
        verdict = "BEATING MARKET ✓" if (alpha or 0) > 0 else "LAGGING MARKET"
        print(f"\n  SPY same period  : {spy:+.1%}")
        print(f"  Our alpha        : {alpha_str}  ← {verdict}")

    # ── Open positions ──
    if summary["positions"]:
        print(f"\n  {'TICKER':<7} {'ENTRY':>8} {'NOW':>8} {'RETURN':>8} "
              f"{'VALUE':>8}  STOP STATUS")
        print("  " + "-" * 58)
        for p in summary["positions"]:
            ticker = p["ticker"]
            pos = portfolio.positions.get(ticker)
            stop_status = ""
            if pos and pos.stop_tier3 and pos.current_price <= pos.stop_tier3:
                stop_status = "⚠ TIER3"
            elif pos and pos.stop_tier1 and pos.current_price <= pos.stop_tier1:
                stop_status = "! TIER1"
            ret_icon = "▲" if p["return_pct"] >= 0 else "▼"
            print(f"  {ticker:<7} ${p['entry_price']:>7.2f} ${p['current_price']:>7.2f} "
                  f"{ret_icon}{abs(p['return_pct']):.1%}  "
                  f"${p['market_value']:>7.2f}  {stop_status}")
    else:
        print("\n  No open positions.")

    # ── Today's actions ──
    if summary["actions_taken"]:
        print(f"\n  TODAY'S ACTIONS:")
        for a in summary["actions_taken"]:
            print(f"    → {a}")

    if summary["alerts"]:
        print(f"\n  ALERTS:")
        for a in summary["alerts"]:
            print(f"    ⚠  {a}")

    # ── P&L trend (last 7 days) ──
    if len(history) >= 2:
        print(f"\n  P&L TREND (last {min(len(history), 10)} days):")
        for snap in history[-10:]:
            bar_val = snap["return_pct"]
            bar_len = int(abs(bar_val) * 200)
            bar = ("█" * min(bar_len, 20)) if bar_val >= 0 else ("░" * min(bar_len, 20))
            direction = "+" if bar_val >= 0 else "-"
            print(f"    {snap['date']}  ${snap['total_value']:>7.2f}  "
                  f"{direction}{abs(bar_val):.1%}  {bar}")

    # ── Trade summary ──
    if trades:
        buys = [t for t in trades if t.action == "BUY"]
        sells = [t for t in trades if t.action != "BUY"]
        closed_pnl = sum(
            (t.price - next((b.price for b in buys if b.ticker == t.ticker), t.price)) * t.shares
            for t in sells
        )
        print(f"\n  TRADE SUMMARY:")
        print(f"    Total buys executed  : {len(buys)}")
        print(f"    Stops / trims hit    : {len(sells)}")
        print(f"    Realized P&L         : ${closed_pnl:+.2f}")

    print("\n" + "=" * 65)


def run_evening_update() -> dict:
    """Execute scan signals, update prices, check stops, print report."""
    # Auto-execute any BUY signals from today's scan
    new_trades = auto_execute_scan_signals()
    if new_trades:
        logger.info(f"Paper trading: auto-executed {len(new_trades)} new positions")
        for t in new_trades:
            print(f"  NEW PAPER POSITION: {t.ticker} @ ${t.price:.2f} "
                  f"({t.shares:.4f} shares = ${t.value:.2f})")

    # Daily update: refresh prices, check stops, profit trims
    summary = daily_update()
    print_report(summary)
    return summary


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Paper trading report")
    parser.add_argument("--reset", action="store_true",
                        help="Reset paper portfolio and start fresh with $500")
    parser.add_argument("--capital", type=float, default=STARTING_CAPITAL,
                        help=f"Starting capital for reset (default: ${STARTING_CAPITAL:.0f})")
    parser.add_argument("--report-only", action="store_true",
                        help="Print report without executing new signals or updating prices")
    args = parser.parse_args()

    if args.reset:
        confirm = input(f"Reset paper portfolio to ${args.capital:.0f}? (yes/no): ")
        if confirm.lower() == "yes":
            reset_paper_portfolio(args.capital)
            print(f"Paper portfolio reset. Starting fresh with ${args.capital:.0f}.")
        else:
            print("Cancelled.")
        sys.exit(0)

    if args.report_only:
        portfolio = load_paper_portfolio()
        history = get_pnl_history()
        from core.paper_trading import _fetch_spy_return_since
        spy = _fetch_spy_return_since(portfolio.start_date) if portfolio.start_date else None
        summary = {
            "date": datetime.date.today().isoformat(),
            "portfolio_value": round(portfolio.total_value, 2),
            "cash": round(portfolio.cash, 2),
            "invested": round(portfolio.invested_value, 2),
            "total_return_pct": round(portfolio.total_return_pct, 4),
            "total_return_dollars": round(portfolio.total_value - portfolio.starting_capital, 2),
            "spy_return_since_start": round(spy, 4) if spy else None,
            "alpha": round(portfolio.total_return_pct - spy, 4) if spy else None,
            "n_positions": len(portfolio.positions),
            "actions_taken": [],
            "alerts": [],
            "positions": [
                {
                    "ticker": t, "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "shares": p.shares,
                    "market_value": round(p.market_value, 2),
                    "return_pct": round(p.return_pct, 4),
                    "unrealized_pnl": round(p.unrealized_pnl, 2),
                }
                for t, p in sorted(portfolio.positions.items(),
                                    key=lambda x: -x[1].return_pct)
            ],
        }
        print_report(summary)
    else:
        run_evening_update()
