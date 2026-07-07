"""
Standalone swing momentum scan — saves results to data/swing_candidates_{date}.json.
Can be run independently of the full daily scan.
Called by daily_scan.py automatically, or run manually:
  python workflows/run_swing_scan.py
"""
import datetime
import json
import logging
import os
import sys

sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def run_swing_scan() -> list[dict]:
    from core.screener import swing_momentum_scan
    from core.momentum_monitor import monitor_open_swings
    from core.sector_map import MICRO_SECTORS
    from core.regime_detector import detect_regime
    from workflows.weekly_scan import load_weekly_universe

    today = datetime.date.today().isoformat()

    # Regime
    try:
        regime_obj = detect_regime()
        regime = regime_obj.regime.value
    except Exception:
        regime = "BULL"

    # Universe: S&P500+Nasdaq100 prefiltered (full 451-stock universe → ~150 liquid movers)
    # Falls back to weekly universe if prefilter returns nothing
    from core.screener import swing_universe_prefilter
    universe = swing_universe_prefilter(max_tickers=150)
    if not universe:
        logger.warning("Prefilter returned 0 stocks — falling back to weekly universe")
        weekly = load_weekly_universe()
        ticker_to_ms: dict[str, str] = {}
        for ms_name, ms_data in MICRO_SECTORS.items():
            for t in ms_data.get("stocks", []):
                if t not in ticker_to_ms:
                    ticker_to_ms[t] = ms_name
        if weekly:
            universe = [(t, ticker_to_ms.get(t, "wildcard")) for t in weekly["all_stocks"]]
        else:
            from core.sector_map import WILDCARD_POOL
            universe = [(t, "wildcard") for t in WILDCARD_POOL]
    logger.info(f"Swing universe: {len(universe)} stocks")

    # ETF returns for RS signal — from weekly scan sector scores
    etf_returns = {}
    weekly = load_weekly_universe()
    if weekly:
        micro_scores = weekly.get("sector_scores", {}).get("micro", {})
        etf_returns = {name: s.get("return_60d", 0.0) for name, s in micro_scores.items()}

    # Swing capital available
    total_swing_capital = 1500.0
    try:
        from core.position_store import capital_overview
        ov = capital_overview()
        total_swing_capital = ov.get("swing_capital_available", 1500.0)
    except Exception:
        pass

    # Run 7-signal scan
    logger.info(f"Running 7-signal swing scan on {len(universe)} stocks (regime={regime})...")
    signals = swing_momentum_scan(
        universe,
        sector_etf_returns=etf_returns,
        regime=regime,
        total_swing_capital=total_swing_capital,
        min_signals=2,
    )

    # Exit alerts on open positions
    alerts = monitor_open_swings()

    # Serialise
    candidates = [
        {
            "ticker": s.ticker,
            "sector": s.sector,
            "microsector": s.microsector,
            "price": s.price,
            "signals_fired": s.signals_fired,
            "signals_score": s.signals_score,
            "conviction": s.conviction,
            "suggested_dollars": s.suggested_dollars,
            "exit_rules": s.exit_rules,
            "notes": s.notes,
        }
        for s in signals
    ]
    exit_alerts = [
        {
            "ticker": a.ticker,
            "reason": a.reason,
            "urgency": a.urgency,
            "current_price": a.current_price,
            "entry_price": a.entry_price,
            "return_pct": a.return_pct,
            "detail": a.detail,
            "action": a.action,
        }
        for a in alerts
    ]

    output = {
        "date": today,
        "regime": regime,
        "total_scanned": len(universe),
        "candidates": candidates,
        "exit_alerts": exit_alerts,
        "generated_at": datetime.datetime.now().isoformat(),
    }

    os.makedirs("data", exist_ok=True)
    path = f"data/swing_candidates_{today}.json"
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    logger.info(f"Swing candidates saved → {path}")

    # Print summary
    print(f"\n{'='*55}")
    print(f"  SWING SCAN — {today} | regime={regime}")
    print(f"{'='*55}")
    print(f"  {len(candidates)} candidates | {len(exit_alerts)} exit alerts\n")
    for c in candidates:
        print(f"  {c['ticker']:<6} [{c['conviction']:6}] {c['signals_score']}/7 "
              f"${c['suggested_dollars']:>6.0f} — {', '.join(c['signals_fired'])}")
    if exit_alerts:
        print(f"\n  ⚠ EXIT ALERTS:")
        for a in exit_alerts:
            print(f"  {a['ticker']}: {a['reason']} [{a['urgency']}] — {a['detail']}")
    print(f"{'='*55}\n")

    return candidates


if __name__ == "__main__":
    run_swing_scan()
