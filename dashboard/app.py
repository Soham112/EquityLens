"""
Dashboard API — FastAPI backend serving the research dashboard.
Endpoints: scan results, single stock analysis, portfolio, journal metrics.
"""
import datetime
import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

app = FastAPI(title="EquityLens", docs_url=None, redoc_url=None)

# Mount static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

DATA_DIR = Path("data")


def _latest_scan() -> Optional[dict]:
    """
    Load the most recent daily scan result. Globs rather than checking
    today/yesterday only — scans run Mon-Fri, so on a Sunday "yesterday"
    (Saturday) never exists and the dashboard went blank despite Friday's
    scan being 3 days old, not missing.
    """
    files = sorted(DATA_DIR.glob("daily_scan_*.json"), reverse=True)
    if not files:
        return None
    with open(files[0]) as f:
        return json.load(f)


def _latest_health() -> Optional[dict]:
    """Same glob-for-most-recent fix as _latest_scan — weekends/holidays
    otherwise leave this returning None even though a recent file exists."""
    files = sorted(DATA_DIR.glob("health_check_*.json"), reverse=True)
    if not files:
        return None
    with open(files[0]) as f:
        return json.load(f)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/api/scan")
async def get_scan():
    scan = _latest_scan()
    if not scan:
        return JSONResponse({
            "results": [],
            "buy_signals": 0,
            "watchlist_signals": 0,
            "total_scanned": 0,
            "empty": True,
            "message": "No scan data yet — next scan runs Mon–Fri at 9:35 AM automatically.",
        })
    return scan


@app.get("/api/names")
async def ticker_names():
    """Ticker → company name map: universe cache (S&P/Nasdaq) + curated growth list."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.universe import load_name_map
    names = load_name_map()
    try:
        from core.growth_universe import get_growth_names
        for t, n in get_growth_names().items():
            names.setdefault(t, n)
    except Exception:
        pass
    return names


@app.get("/api/health")
async def get_health():
    health = _latest_health()
    if not health:
        return {"status": "No health check run today", "regime": "UNKNOWN"}
    return health


@app.get("/api/stock/{ticker}")
async def get_stock(ticker: str):
    scan = _latest_scan()
    if not scan:
        raise HTTPException(status_code=404, detail="No scan data")
    results = scan.get("results", [])
    match = next((r for r in results if r["ticker"].upper() == ticker.upper()), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"{ticker} not in latest scan")
    return match


@app.post("/api/analyze/{ticker}")
async def analyze_stock(ticker: str, sector: str = Query(default="technology")):
    """Run a live analysis on a single ticker."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.orchestrator import analyze
    result = analyze(ticker.upper(), sector)
    if not result:
        raise HTTPException(status_code=500, detail=f"Analysis failed for {ticker}")
    return {
        "ticker": result.ticker,
        "signal": result.signal,
        "conviction": result.conviction,
        "data_confidence": result.data_confidence,
        "hunter_score": result.hunter_score,
        "sentiment_boost": result.sentiment_boost,
        "data_quality": result.data_quality,
        "red_flags": result.red_flags,
        "kill_switch": result.kill_switch,
        "regime": result.regime,
        "stop_tier1": result.stop_tier1,
        "stop_tier2": result.stop_tier2,
        "stop_tier3": result.stop_tier3,
        "earnings_phase": result.earnings_phase,
        "days_to_earnings": result.days_to_earnings,
        "recommended_position_pct": result.recommended_position_pct,
        "recommended_position_dollars": result.recommended_position_dollars,
        "thesis": result.thesis,
        "alerts": result.alerts,
    }


@app.get("/api/conviction/history/{ticker}")
async def conviction_history(ticker: str, days: int = Query(default=30)):
    """Conviction score history for a ticker — used for dashboard trend charts."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.persistence import get_conviction_series
    series = get_conviction_series(ticker.upper(), days=days)
    return {"ticker": ticker.upper(), "series": series}


@app.get("/api/portfolio")
async def get_portfolio():
    """Current tracked portfolio positions."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.persistence import load_portfolio
    return load_portfolio()


@app.get("/api/paper/portfolio")
async def paper_portfolio():
    """Current paper trading portfolio — positions, cash, total return."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.paper_trading import load_paper_portfolio, _fetch_spy_return_since, _fetch_price
    import datetime as dt
    p = load_paper_portfolio()
    # Refresh live prices for all positions
    for ticker, pos in p.positions.items():
        live = _fetch_price(ticker)
        if live:
            pos.current_price = live
    spy = _fetch_spy_return_since(p.start_date) if p.start_date else None
    return {
        "start_date": p.start_date,
        "starting_capital": p.starting_capital,
        "cash": round(p.cash, 2),
        "invested_value": round(p.invested_value, 2),
        "total_value": round(p.total_value, 2),
        "total_return_pct": round(p.total_return_pct, 4),
        "total_return_dollars": round(p.total_value - p.starting_capital, 2),
        "spy_return": round(spy, 4) if spy else None,
        "alpha": round(p.total_return_pct - spy, 4) if spy else None,
        "n_positions": len(p.positions),
        "positions": [
            {
                "ticker": t,
                "entry_price": pos.entry_price,
                "current_price": pos.current_price,
                "shares": pos.shares,
                "market_value": round(pos.market_value, 2),
                "return_pct": round(pos.return_pct, 4),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "entry_date": pos.entry_date,
                "conviction": pos.conviction,
                "stop_tier1": pos.stop_tier1,
                "stop_tier2": pos.stop_tier2,
                "stop_tier3": pos.stop_tier3,
            }
            for t, pos in sorted(p.positions.items(), key=lambda x: -x[1].return_pct)
        ],
    }


@app.get("/api/paper/pnl-history")
async def paper_pnl_history():
    """Daily P&L snapshots for chart rendering."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.paper_trading import get_pnl_history
    return {"history": get_pnl_history()}


@app.get("/api/paper/trades")
async def paper_trades():
    """Full paper trade history."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.paper_trading import load_trade_history
    from dataclasses import asdict
    return {"trades": [asdict(t) for t in load_trade_history()]}


@app.post("/api/paper/reset")
async def paper_reset(capital: float = Query(default=500.0)):
    """Reset paper portfolio to starting capital."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.paper_trading import reset_paper_portfolio
    reset_paper_portfolio(capital)
    return {"status": "reset", "starting_capital": capital}


@app.get("/api/universe/stats")
async def universe_stats_endpoint():
    """Current universe cache: size, sector breakdown, freshness."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.universe import universe_stats
    return universe_stats()


@app.get("/api/universe/scan-list")
async def universe_scan_list(max_tickers: int = Query(default=125)):
    """Today's tiered scan list (Tier 1 + Tier 2 bucket)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.universe import build_scan_list
    tickers = build_scan_list(max_tickers=max_tickers)
    return {"count": len(tickers), "tickers": [{"ticker": t, "sector": s} for t, s in tickers]}


@app.get("/api/backtest/latest")
async def backtest_latest():
    """Return the most recent backtest report (pre-computed)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.backtest import load_latest_backtest
    report = load_latest_backtest()
    if not report:
        return JSONResponse(
            {"error": "No backtest data. Run: python -m core.backtest"},
            status_code=404,
        )
    return report


@app.post("/api/backtest/run")
async def run_backtest(hold_days: str = Query(default="5,10,20,60")):
    """Run signal-replay backtest on stored scan files. Returns summary string."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.backtest import BacktestConfig, run_signal_replay
    days = [int(d.strip()) for d in hold_days.split(",")]
    config = BacktestConfig(hold_days=days)
    report = run_signal_replay(config=config, save_report=True)
    return {"summary": report.summary(), "total_signals": report.total_signals}


@app.get("/api/portfolio/correlation")
async def portfolio_correlation():
    """Correlation report for currently tracked portfolio positions."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.correlation import portfolio_correlation_report
    from core.persistence import load_portfolio
    positions = load_portfolio()
    if not positions:
        return {"message": "No portfolio positions tracked yet"}
    held_pcts = {t: p.get("position_pct", 0) for t, p in positions.items()}
    return portfolio_correlation_report(held_pcts)


@app.get("/api/backtest/baseline")
async def backtest_baseline():
    """Latest BUY signal alpha vs SPY baseline."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from pathlib import Path as P
    import json as _json
    files = sorted(P("data").glob("baseline_comparison_*.json"), reverse=True)
    if not files:
        return JSONResponse({"error": "No baseline comparison yet. Run the daily scan first."}, status_code=404)
    with open(files[0]) as f:
        return _json.load(f)


@app.get("/api/bias")
async def bias_report():
    """Run behavioral bias checks against the journal and return flags."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.bias_check import scan_for_biases
    report = scan_for_biases()
    return report.to_dict()


@app.get("/api/bias/pre-decision/{ticker}")
async def bias_pre_decision(ticker: str, price: float = Query(...)):
    """Pre-decision bias check for a specific ticker at a given price."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.bias_check import analyze_pre_decision
    report = analyze_pre_decision(ticker.upper(), current_price=price)
    return report.to_dict()


@app.get("/api/decisions/pending")
async def pending_decisions():
    """BUY signals awaiting your investment decision."""
    import sys, json as _json
    sys.path.insert(0, str(Path(__file__).parent.parent))
    pf = Path("data") / "pending_decisions.json"
    if not pf.exists():
        return {"pending": []}
    return {"pending": _json.loads(pf.read_text())}


@app.post("/api/decisions/invest/{ticker}")
async def record_invest(ticker: str, entry_price: float = Query(...),
                        shares: float = Query(...), position_pct: float = Query(default=0.05),
                        notes: str = Query(default="")):
    """Record that you invested in a BUY signal."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from workflows.decision_capture import record_investment
    record_investment(ticker.upper(), entry_price, shares, position_pct, notes)
    return {"status": "recorded", "ticker": ticker.upper(), "entry_price": entry_price}


@app.get("/api/decisions/daily-log")
async def decisions_daily_log(days: int = Query(default=30)):
    """
    Daily activity log: for each scan day, what BUY signals fired, what the paper
    portfolio entered/exited, and what happened to swing positions.
    Reads from daily_scan_{date}.json files.
    """
    import glob, json as _json, math

    def _clean(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    scan_files = sorted(
        glob.glob(str(DATA_DIR / "daily_scan_*.json")),
        reverse=True
    )[:days]

    log = []
    for path in scan_files:
        try:
            with open(path) as f:
                scan = _json.load(f)
        except Exception:
            continue

        date_str = Path(path).stem.replace("daily_scan_", "")
        results = scan.get("results", [])
        buys      = [r for r in results if r.get("signal") == "BUY"]
        watchlist = [r for r in results if r.get("signal") == "WATCHLIST"]
        swing_cands = scan.get("swing_candidates", [])
        swing_exits = scan.get("swing_exit_alerts", [])

        log.append({
            "date": date_str,
            "regime": scan.get("regime", ""),
            "vix": _clean(scan.get("vix")),
            "total_scanned": scan.get("total_scanned", 0),
            "buy_count": len(buys),
            "watchlist_count": len(watchlist),
            "buys": [
                {
                    "ticker": r["ticker"],
                    "conviction": _clean(r.get("conviction")),
                    "thesis": (r.get("thesis") or "")[:120],
                    "stop_tier1": _clean(r.get("stop_tier1")),
                    "stop_tier3": _clean(r.get("stop_tier3")),
                }
                for r in sorted(buys, key=lambda x: -(x.get("conviction") or 0))
            ],
            "watchlist": [
                {"ticker": r["ticker"], "conviction": _clean(r.get("conviction"))}
                for r in sorted(watchlist, key=lambda x: -(x.get("conviction") or 0))[:8]
            ],
            "swing_candidates": [
                {
                    "ticker": s["ticker"],
                    "conviction": s.get("conviction"),
                    "signals_score": s.get("signals_score"),
                    "entry_type": s.get("entry_type"),
                    "risk_reward": _clean(s.get("risk_reward")),
                }
                for s in swing_cands[:8]
            ],
            "swing_exit_alerts": [
                {
                    "ticker": a.get("ticker"),
                    "reason": a.get("reason"),
                    "urgency": a.get("urgency"),
                    "return_pct": _clean(a.get("return_pct")),
                }
                for a in swing_exits
            ],
            "new_buys_paused": scan.get("new_buys_paused", False),
        })

    return {"log": log}


@app.post("/api/decisions/skip/{ticker}")
async def record_skip_decision(ticker: str, reason: str = Query(default="")):
    """Record that you skipped a BUY signal."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from workflows.decision_capture import record_skip
    record_skip(ticker.upper(), reason)
    return {"status": "skipped", "ticker": ticker.upper()}


@app.get("/api/review/weekly")
async def weekly_review():
    """Latest weekly outcome review."""
    files = sorted(Path("data").glob("weekly_review_*.json"), reverse=True)
    if not files:
        return JSONResponse({"error": "No weekly review yet. Run workflows/outcome_review.py"}, status_code=404)
    import json as _json
    return _json.loads(files[0].read_text())


@app.get("/api/swing/candidates")
async def swing_candidates():
    """Latest 7-signal swing candidates (from run_swing_scan.py or daily scan)."""
    import glob, json as _json
    files = sorted(glob.glob(str(DATA_DIR / "swing_candidates_*.json")), reverse=True)
    if not files:
        return JSONResponse({"candidates": [], "exit_alerts": [], "empty": True,
                             "message": "No swing scan yet. Run: python workflows/run_swing_scan.py"})
    return _json.loads(open(files[0]).read())


@app.get("/api/scan/weekly")
async def weekly_scan():
    """Latest weekly universe (sector funnel output from Sunday scan)."""
    files = sorted(Path("data").glob("weekly_universe_*.json"), reverse=True)
    if not files:
        return JSONResponse({"error": "No weekly scan yet. Run workflows/weekly_scan.py"}, status_code=404)
    import json as _json
    return _json.loads(files[0].read_text())


## ── Growth Scout endpoints ─────────────────────────────────────────────────

def _latest_growth_scan() -> Optional[dict]:
    import datetime as _dt
    today = _dt.date.today().isoformat()
    for date_str in [today, (_dt.date.today() - _dt.timedelta(days=1)).isoformat()]:
        path = DATA_DIR / f"growth_scan_{date_str}.json"
        if path.exists():
            with open(path) as f:
                return json.load(f)
    return None


@app.get("/api/growth/scan")
async def growth_scan():
    # DEPRECATED 2026-07-05: the standalone growth_scan.py pipeline was merged
    # into daily_scan.py (see core.screener.growth_hunter_candidates). Its
    # candidates now appear in /api/swing/candidates alongside the 7-signal
    # results — look for notes containing "GrowthHunter". This endpoint is
    # kept only for old cached files; no frontend tab calls it.
    scan = _latest_growth_scan()
    if not scan:
        return JSONResponse({
            "results": [],
            "speculative_buy_count": 0,
            "watch_count": 0,
            "total_scanned": 0,
            "empty": True,
            "message": "Growth Scout no longer runs as a separate scan — its candidates are "
                       "merged into /api/swing/candidates (look for 'GrowthHunter' in notes).",
        })
    # Sanitize NaN/Inf values that break JSON serialization
    import math
    def _clean(obj):
        if isinstance(obj, float):
            return None if (math.isnan(obj) or math.isinf(obj)) else obj
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(i) for i in obj]
        return obj
    return JSONResponse(_clean(scan))


@app.get("/api/growth/portfolio")
async def growth_portfolio():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.growth_paper_trading import load_growth_portfolio
    p = load_growth_portfolio()
    return {
        "starting_capital": p.starting_capital,
        "cash": round(p.cash, 2),
        "invested_value": round(p.invested_value, 2),
        "total_value": round(p.total_value, 2),
        "total_return_pct": round(p.total_return_pct, 4),
        "total_return_dollars": round(p.total_value - p.starting_capital, 2),
        "n_positions": len(p.positions),
        "start_date": p.start_date,
        "positions": [
            {
                "ticker": t,
                "entry_price": pos.entry_price,
                "current_price": pos.current_price,
                "shares": pos.shares,
                "market_value": round(pos.market_value, 2),
                "return_pct": round(pos.return_pct, 4),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "entry_date": pos.entry_date,
                "growth_score": pos.growth_score,
                "sector": pos.sector,
                "stop_price": pos.stop_price,
                "peak_price": pos.peak_price,
            }
            for t, pos in sorted(p.positions.items(), key=lambda x: -x[1].return_pct)
        ],
    }


@app.get("/api/growth/trades")
async def growth_trades():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.growth_paper_trading import load_growth_trades
    return {"trades": load_growth_trades()}


## ── Swing / Long-Term / DCA endpoints ──────────────────────────────────────

@app.get("/api/swing/positions")
async def swing_positions(status: str = Query(default="OPEN")):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import load_swing_positions
    import yfinance as yf
    positions = load_swing_positions(status.upper())
    result = []
    for p in positions:
        if p.status == "OPEN":
            try:
                live = float(yf.Ticker(p.ticker).history(period="1d")["Close"].iloc[-1])
                p.current_price = live
                gain_pct = (live - p.entry_price) / p.entry_price
                p.peak_price = max(p.peak_price, live)
                if p.track == "MOMENTUM" and p.exit_plan.get("trailing_stop_pct"):
                    p.stop_price = round(p.peak_price * (1 - p.exit_plan["trailing_stop_pct"]), 2)
            except Exception:
                gain_pct = 0.0
        else:
            # EXITED — use stored exit_price if available, else current_price
            exit_price = p.exit_plan.get("exit_price") or p.current_price or p.entry_price
            gain_pct = (exit_price - p.entry_price) / p.entry_price if p.entry_price else 0.0
        ep = p.exit_plan
        result.append({
            "ticker": p.ticker,
            "sector": p.sector,
            "track": p.track,
            "pattern": p.pattern,
            "pattern_confidence": p.pattern_confidence,
            "price_structure": p.price_structure,
            "entry_price": p.entry_price,
            "entry_date": p.entry_date,
            "exit_date": ep.get("exit_date"),
            "exit_reason": ep.get("exit_reason"),
            "current_price": p.current_price,
            "gain_pct": round(gain_pct, 4),
            "invested_dollars": p.invested_dollars,
            "market_value": round(p.current_price * p.shares, 2) if p.status == "OPEN" else None,
            "unrealized_pnl": round((p.current_price - p.entry_price) * p.shares, 2) if p.status == "OPEN" else None,
            "realized_pnl": round(gain_pct * p.invested_dollars, 2) if p.status != "OPEN" else None,
            "days_held": p.days_held,
            "target_price": p.target_price,
            "target_pct": ep.get("hard_target_pct"),
            "stop_price": p.stop_price,
            "stop_pct": ep.get("stop_loss_pct"),
            "trailing_stop_pct": ep.get("trailing_stop_pct"),
            "peak_price": p.peak_price,
            "pattern_thesis": p.pattern_thesis,
            "screens_matched": p.screens_matched,
            "promotion_eligible": p.promotion_eligible,
            "invalidation_note": ep.get("invalidation_note", ""),
            "status": p.status,
        })
    return {"positions": result, "count": len(result)}


@app.post("/api/swing/add")
async def add_swing_position(
    ticker: str = Query(...),
    sector: str = Query(default="technology"),
    entry_price: float = Query(...),
    dollars: float = Query(...),
    pattern: str = Query(default="none"),
    thesis: str = Query(default=""),
    track: str = Query(default="SWING"),
):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import SwingPosition, save_swing_position
    import datetime as _dt
    shares = dollars / entry_price
    target_price = round(entry_price * 1.18, 2) if track == "SWING" else None
    stop_price = round(entry_price * 0.93, 2)
    pos = SwingPosition(
        ticker=ticker.upper(), sector=sector, track=track,
        entry_price=entry_price, entry_date=_dt.date.today().isoformat(),
        shares=shares, invested_dollars=dollars,
        pattern=pattern, pattern_confidence=0.0,
        price_structure="BASING", screens_matched=[],
        exit_plan={"hard_target_pct": 0.18, "stop_loss_pct": 0.07,
                   "trailing_stop_pct": None, "invalidation_note": "Manual entry"},
        pattern_thesis=thesis,
        peak_price=entry_price, current_price=entry_price,
        stop_price=stop_price, target_price=target_price,
    )
    save_swing_position(pos)
    return {"status": "added", "ticker": ticker.upper(), "track": track}


@app.post("/api/swing/exit/{ticker}")
async def exit_swing(ticker: str, exit_price: float = Query(...), reason: str = Query(default="manual")):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import exit_swing_position
    from core.feedback import record_exit
    ok = exit_swing_position(ticker.upper(), exit_price, reason)
    if ok:
        record_exit(ticker.upper(), exit_price, reason)
    return {"status": "exited" if ok else "not_found", "ticker": ticker.upper()}


@app.post("/api/swing/promote/{ticker}")
async def promote_swing(ticker: str, new_track: str = Query(...)):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import promote_swing_position
    new_exit = {"trailing_stop_pct": 0.12, "stop_loss_pct": 0.08,
                "hard_target_pct": None, "invalidation_note": "Trail stop below peak"}
    ok = promote_swing_position(ticker.upper(), new_track, new_exit)
    return {"status": "promoted" if ok else "not_found", "ticker": ticker.upper(), "new_track": new_track}


@app.get("/api/longterm/positions")
async def longterm_positions():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import load_longterm_positions
    import yfinance as yf
    positions = load_longterm_positions("OPEN")
    result = []
    for p in positions:
        try:
            live = float(yf.Ticker(p.ticker).history(period="1d")["Close"].iloc[-1])
            p.current_price = live
        except Exception:
            pass
        gain_pct = (p.current_price - p.avg_cost_basis) / p.avg_cost_basis if p.avg_cost_basis else 0
        result.append({
            "ticker": p.ticker,
            "sector": p.sector,
            "entry_date": p.entry_date,
            "avg_cost_basis": p.avg_cost_basis,
            "current_price": p.current_price,
            "total_shares": p.total_shares,
            "total_invested": p.total_invested,
            "market_value": round(p.current_price * p.total_shares, 2),
            "unrealized_pnl": round((p.current_price - p.avg_cost_basis) * p.total_shares, 2),
            "gain_pct": round(gain_pct, 4),
            "hunter_score_at_entry": p.hunter_score_at_entry,
            "thesis": p.thesis,
            "dca_amount": p.dca_amount,
            "next_dca_date": p.next_dca_date,
            "add_count": len(p.adds),
        })
    return {"positions": result, "count": len(result)}


@app.post("/api/longterm/add")
async def add_longterm_position(
    ticker: str = Query(...),
    sector: str = Query(default="technology"),
    entry_price: float = Query(...),
    dollars: float = Query(...),
    hunter_score: float = Query(default=0.0),
    thesis: str = Query(default=""),
    dca_amount: float = Query(default=50.0),
):
    import sys, datetime as _dt
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import LongTermPosition, save_longterm_position
    today = _dt.date.today()
    next_month = today.replace(day=1)
    if next_month.month == 12:
        next_dca = today.replace(year=today.year + 1, month=1, day=1)
    else:
        next_dca = today.replace(month=today.month + 1, day=1)
    shares = dollars / entry_price
    pos = LongTermPosition(
        ticker=ticker.upper(), sector=sector,
        entry_price=entry_price, entry_date=today.isoformat(),
        total_shares=shares, total_invested=dollars,
        avg_cost_basis=entry_price, current_price=entry_price,
        hunter_score_at_entry=hunter_score, thesis=thesis,
        dca_amount=dca_amount, dca_day_of_month=1,
        next_dca_date=next_dca.isoformat(),
    )
    save_longterm_position(pos)
    return {"status": "added", "ticker": ticker.upper()}


@app.post("/api/longterm/dca/{ticker}")
async def dca_add(ticker: str, price: float = Query(...), dollars: float = Query(...)):
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import add_dca_to_longterm
    ok = add_dca_to_longterm(ticker.upper(), price, dollars)
    return {"status": "added" if ok else "not_found", "ticker": ticker.upper()}


@app.get("/api/capital/overview")
async def capital_overview_endpoint():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import capital_overview, UNIFIED_CAPITAL
    return capital_overview(UNIFIED_CAPITAL)


@app.get("/api/trades/closed")
async def trades_closed():
    """Unified realized-trade history across both paper portfolios."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.position_store import list_closed_trades
    trades = list_closed_trades()
    realized = sum(t["pnl"] for t in trades if t["pnl"] is not None)
    return {"trades": trades, "realized_pnl": round(realized, 2)}


## ── Feedback loop endpoints ─────────────────────────────────────────────────

@app.get("/api/feedback/summary")
async def feedback_summary():
    """Full weekly feedback — screen hit rates, mistake patterns, trend."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.feedback import weekly_feedback_summary
    return weekly_feedback_summary()


@app.get("/api/feedback/screens")
async def feedback_screens(min_trades: int = Query(default=3)):
    """Per-screen hit rate and performance stats."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.feedback import screen_report
    from dataclasses import asdict
    return {"screens": [asdict(s) for s in screen_report(min_trades=min_trades)]}


@app.get("/api/feedback/mistakes")
async def feedback_mistakes():
    """Recurring mistake patterns detected from closed trades."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.feedback import mistake_report
    from dataclasses import asdict
    return {"mistakes": [asdict(m) for m in mistake_report()]}


@app.get("/api/discovery")
async def discovery():
    """E15: Super-Performer discovery shortlist (mid/small caps, Trend Template)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.discovery import load_latest_discovery
    return load_latest_discovery()


@app.get("/api/backtest/sectors")
async def backtest_sectors(refresh: bool = Query(default=False)):
    """E9 Phase 1: 5-year sector funnel backtest (cached; refresh=true recomputes)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.sector_backtest import load_or_run
    return load_or_run(refresh=refresh)


@app.get("/api/feedback/shadow")
async def feedback_shadow():
    """E8 shadow tracking: gate-demoted signal cohorts vs entered BUYs (30/90d)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.signal_tracker import shadow_gate_report
    return shadow_gate_report()


@app.get("/api/feedback/gates")
async def feedback_gates():
    """Exploration-mode gate state + per-gate cohort scoreboard + adaptation history."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config.settings import settings
    from core.feedback import load_gate_state, gate_cohort_report
    state = load_gate_state()
    return {
        "mode": getattr(settings, "swing_entry_mode", "strict"),
        "gates": {g: state.get(g) for g in ("signals", "risk_reward", "entry_zone")},
        "cohorts": gate_cohort_report(),
        "history": state.get("history", []),
    }


@app.post("/api/feedback/exit/{ticker}")
async def feedback_exit(ticker: str, exit_price: float = Query(...), reason: str = Query(default="manual")):
    """Record an exit outcome — links to open signal record for this ticker."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from core.feedback import record_exit
    ok = record_exit(ticker.upper(), exit_price, reason)
    return {"status": "recorded" if ok else "no_open_signal", "ticker": ticker.upper()}


@app.get("/api/swing/chart/{ticker}")
async def swing_chart(ticker: str, analyze: bool = Query(default=False)):
    """
    Return swing chart image + analysis JSON for a ticker.
    GET /api/swing/chart/NVDA         → return existing chart (from today's scan)
    GET /api/swing/chart/NVDA?analyze=true → regenerate chart + run Vision analysis
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))

    ticker = ticker.upper()
    chart_dir = DATA_DIR / "swing_charts"
    today = datetime.date.today().isoformat()

    # Look for today's chart on disk first (from daily scan)
    chart_path = chart_dir / f"{ticker}_{today}.png"
    if chart_path.exists() and not analyze:
        # Check if we have analysis cached in today's daily scan
        scan = _latest_scan()
        if scan:
            for candidate in scan.get("swing_candidates", []):
                if candidate.get("ticker") == ticker and candidate.get("entry_type"):
                    return JSONResponse({
                        "ticker": ticker,
                        "chart_url": f"/api/swing/chart/{ticker}/image",
                        "entry_type":         candidate.get("entry_type"),
                        "pattern":            candidate.get("pattern"),
                        "pattern_confidence": candidate.get("pattern_confidence"),
                        "entry_zone_low":     candidate.get("entry_zone_low"),
                        "entry_zone_high":    candidate.get("entry_zone_high"),
                        "stop_level":         candidate.get("stop_level"),
                        "target_level":       candidate.get("target_level"),
                        "risk_reward":        candidate.get("risk_reward"),
                        "support_levels":     candidate.get("support_levels", []),
                        "resistance_levels":  candidate.get("resistance_levels", []),
                        "chart_thesis":       candidate.get("chart_thesis"),
                    })
            # Also check long-term BUY results — they carry chart analysis in lt_chart
            for result in scan.get("results", []):
                lt = result.get("lt_chart")
                if result.get("ticker") == ticker and lt and lt.get("entry_type"):
                    return JSONResponse({
                        "ticker": ticker,
                        "chart_url": f"/api/swing/chart/{ticker}/image?variant=lt",
                        "timeframe": "weekly (1y) — long-term",
                        "entry_type":         lt.get("entry_type"),
                        "pattern":            lt.get("pattern"),
                        "pattern_confidence": lt.get("pattern_confidence"),
                        "entry_zone_low":     lt.get("entry_zone_low"),
                        "entry_zone_high":    lt.get("entry_zone_high"),
                        "stop_level":         lt.get("stop_level"),
                        "target_level":       lt.get("target_level"),
                        "risk_reward":        lt.get("risk_reward"),
                        "support_levels":     lt.get("support_levels", []),
                        "resistance_levels":  lt.get("resistance_levels", []),
                        "chart_thesis":       lt.get("chart_thesis"),
                    })
        # Chart exists but no cached analysis — still return image URL
        return JSONResponse({"ticker": ticker, "chart_url": f"/api/swing/chart/{ticker}/image"})

    # Run fresh analysis
    try:
        from core.swing_chart_analysis import analyze_swing_candidate
        chart_sig = analyze_swing_candidate(ticker)
        if chart_sig is None:
            raise HTTPException(status_code=404, detail=f"Could not analyze {ticker}")
        return JSONResponse({
            "ticker": ticker,
            "chart_url": f"/api/swing/chart/{ticker}/image",
            "entry_type":         chart_sig.entry_type,
            "pattern":            chart_sig.pattern,
            "pattern_confidence": chart_sig.pattern_confidence,
            "entry_zone_low":     chart_sig.entry_zone_low,
            "entry_zone_high":    chart_sig.entry_zone_high,
            "stop_level":         chart_sig.stop_level,
            "target_level":       chart_sig.target_level,
            "risk_reward":        chart_sig.risk_reward,
            "support_levels":     chart_sig.support_levels,
            "resistance_levels":  chart_sig.resistance_levels,
            "chart_thesis":       chart_sig.chart_thesis,
            "analyzed_at":        chart_sig.analyzed_at,
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/swing/chart/{ticker}/image")
async def swing_chart_image(ticker: str, variant: str = Query(default="")):
    """Serve the chart PNG for a ticker. variant=lt serves the weekly long-term chart."""
    ticker = ticker.upper()
    today = datetime.date.today().isoformat()
    suffix = "_LT" if variant == "lt" else ""
    chart_dir = DATA_DIR / "swing_charts"
    chart_path = chart_dir / f"{ticker}{suffix}_{today}.png"
    if not chart_path.exists():
        # Fall back: other variant from today, then most recent of any kind
        alt = chart_dir / f"{ticker}{'' if suffix else '_LT'}_{today}.png"
        if alt.exists():
            chart_path = alt
        else:
            charts = sorted(chart_dir.glob(f"{ticker}_*.png"), reverse=True)
            if not charts:
                raise HTTPException(status_code=404, detail=f"No chart for {ticker}")
            chart_path = charts[0]
    return FileResponse(str(chart_path), media_type="image/png")


@app.get("/api/journal/metrics")
async def journal_metrics():
    journal_path = DATA_DIR / "journal.jsonl"
    if not journal_path.exists():
        return {"message": "No journal data yet"}
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from agents.journal import calculate_drift, load_records
    records = load_records(days_back=90)
    report = calculate_drift(records)
    return {
        "total_closed": report.total_closed,
        "hit_rate": round(report.hit_rate, 3),
        "payoff_ratio": round(report.payoff_ratio, 2),
        "false_positive_rate": round(report.false_positive_rate, 3),
        "drift_alert": report.drift_alert,
        "drift_delta": round(report.drift_delta, 3),
        "summary": report.summary,
    }
