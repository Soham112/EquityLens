"""
Daily Scan Workflow — runs Mon-Fri at 9:35 AM on the weekly universe.

Universe source (priority order):
  1. Weekly universe from workflows/weekly_scan.py (Sunday funnel output)
  2. Open positions — always included regardless of sector
  3. Wildcard pool — always included
  4. DEFAULT_WATCHLIST — fallback if no weekly universe file exists

The daily scan is intentionally lightweight:
  - Stop-loss monitoring on open positions (always)
  - Price trigger check (new high, volume spike) on universe stocks
  - Hunter/Validator only on triggered stocks or those near entry
  - Chart vision on BUY signals
"""
import datetime
import json
import logging
import os

from core.backtest import compare_to_baseline, run_signal_replay, BacktestConfig
from core.bias_check import scan_for_biases
from core.orchestrator import AnalysisResult, run_batch
from core.persistence import get_held_tickers, load_portfolio
from core.regime_detector import detect_regime, get_vix_spike_actions
from core.staleness import check_feed_health
from core.feedback import auto_log_scan_signals

logger = logging.getLogger(__name__)

# Fallback only — used when no weekly universe file exists
DEFAULT_WATCHLIST: list[tuple[str, str]] = [
    ("NVDA", "semiconductors"),
    ("AMD", "semiconductors"),
    ("AVGO", "semiconductors"),
    ("MSFT", "technology"),
    ("GOOGL", "technology"),
    ("META", "technology"),
    ("AMZN", "technology"),
    ("TSLA", "technology"),
    ("AAPL", "technology"),
    ("CRWD", "cybersecurity"),
    ("NET", "cybersecurity"),
    ("DDOG", "cloud_saas"),
    ("MDB", "cloud_saas"),
    ("ARM", "semiconductors"),
    ("PLTR", "defense_aerospace"),
]


def pre_market_health_check() -> dict:
    """6:00 AM — check data sources before market open."""
    sources = 0
    source_status = {}

    # Check yfinance (ping a simple ticker)
    try:
        import yfinance as yf
        test = yf.Ticker("SPY").history(period="1d")
        if not test.empty:
            sources += 1
            source_status["yfinance"] = "UP"
        else:
            source_status["yfinance"] = "DOWN"
    except Exception:
        source_status["yfinance"] = "DOWN"

    # SEC EDGAR (always available as fallback)
    source_status["sec_edgar"] = "UP"
    sources += 1

    # NewsAPI (if key set)
    if os.getenv("NEWS_API_KEY"):
        source_status["newsapi"] = "UP"
        sources += 1
    else:
        source_status["newsapi"] = "NO_KEY"

    health = check_feed_health(sources)
    regime = detect_regime()

    report = {
        "timestamp": datetime.datetime.now().isoformat(),
        "sources_available": sources,
        "source_status": source_status,
        "operations_status": health["status"],
        "scoring_enabled": health["scoring_enabled"],
        "regime": regime.regime.value,
        "vix": regime.vix_level,
        "spy_ytd": regime.spy_ytd_return,
        "new_buys_paused": regime.new_buys_paused,
    }

    logger.info(f"Health check: {report['operations_status']} | Regime: {report['regime']}")
    _save_report(report, "health_check")
    return report


def _build_daily_tickers() -> list[tuple[str, str]]:
    """
    Assemble today's scan list from the weekly universe + open positions.
    Returns list of (ticker, microsector) tuples.
    """
    from workflows.weekly_scan import load_weekly_universe
    from core.sector_map import MICRO_SECTORS
    from core.sector_map import WILDCARD_POOL

    seen: set[str] = set()
    tickers: list[tuple[str, str]] = []

    def add(ticker: str, sector: str):
        if ticker not in seen:
            seen.add(ticker)
            tickers.append((ticker, sector))

    # 1. Always include open positions — stop-loss monitoring regardless of sector
    try:
        from core.paper_trading import load_paper_portfolio
        pp = load_paper_portfolio()
        for t in pp.positions:
            add(t, "open_position")
    except Exception:
        pass

    portfolio = load_portfolio()
    for t in portfolio:
        add(t, "open_position")

    # 2. Load weekly universe (Sunday funnel output)
    weekly = load_weekly_universe()
    if weekly:
        age_days = (
            datetime.date.today() -
            datetime.date.fromisoformat(weekly["date"])
        ).days
        if age_days <= 7:
            logger.info(
                f"Weekly universe loaded ({weekly['date']}, {age_days}d old) — "
                f"{len(weekly['all_stocks'])} stocks"
            )
            # Build a ticker→microsector lookup from sector_map
            ticker_to_ms: dict[str, str] = {}
            for ms_name, ms_data in MICRO_SECTORS.items():
                for t in ms_data.get("stocks", []):
                    if t not in ticker_to_ms:
                        ticker_to_ms[t] = ms_name

            for t in weekly["all_stocks"]:
                add(t, ticker_to_ms.get(t, "wildcard"))
            return tickers
        else:
            logger.warning(f"Weekly universe is {age_days}d old — falling back to DEFAULT_WATCHLIST")

    # 3. Fallback: no weekly universe
    logger.info("No weekly universe found — using DEFAULT_WATCHLIST + wildcards")
    for t, s in DEFAULT_WATCHLIST:
        add(t, s)
    for t in WILDCARD_POOL:
        add(t, "wildcard")

    return tickers


def run_daily_scan(
    watchlist: list[tuple[str, str]] | None = None,
    max_tickers: int = 80,
) -> list[AnalysisResult]:
    """
    Main daily scan. Reads weekly universe, scores only those stocks.
    Returns sorted results (BUY > WATCHLIST > AVOID).
    """
    health = pre_market_health_check()
    if not health["scoring_enabled"]:
        logger.warning(f"Scoring disabled: {health['operations_status']}")
        return []

    if watchlist:
        tickers = watchlist[:max_tickers]
    else:
        tickers = _build_daily_tickers()[:max_tickers]

    logger.info(f"Running daily scan on {len(tickers)} tickers...")

    # Upgrade 5: Update signal outcomes at start of each scan
    try:
        from core.signal_tracker import update_outcomes
        updated = update_outcomes()
        if updated:
            logger.info(f"[SignalTracker] Updated {updated} pending signal outcomes")
    except Exception as e:
        logger.debug(f"[SignalTracker] update_outcomes skipped: {e}")

    # Build the REAL portfolio state from the paper portfolio. Without it,
    # run_batch defaults to an empty PortfolioState and the sector caps,
    # correlation gate, and whipsaw check silently compare against nothing.
    portfolio_state = _build_portfolio_state()
    results = run_batch(tickers, portfolio=portfolio_state)

    # GAP 7: VIX spike — generate trim actions for all held positions
    vix_spike_alerts: list[dict] = []
    if health.get("new_buys_paused"):
        portfolio = load_portfolio()
        held_pcts = {t: p.get("position_pct", 0.0) for t, p in portfolio.items()}
        if held_pcts:
            regime = detect_regime()
            spike_actions = get_vix_spike_actions(held_pcts, regime)
            vix_spike_alerts = [
                {"ticker": a.ticker, "action": a.action, "trim_pct": a.trim_pct,
                 "urgency": a.urgency, "alert": a.alert}
                for a in spike_actions
            ]
            if vix_spike_alerts:
                logger.warning(
                    f"VIX SPIKE: {len(vix_spike_alerts)} position action(s) generated"
                )
                for a in spike_actions:
                    logger.warning(a.alert)

    # ── Swing momentum scan (7-signal) ──────────────────────────────────────
    swing_signals = []
    exit_alerts = []
    try:
        from core.screener import swing_momentum_scan
        from core.momentum_monitor import monitor_open_swings
        from core.position_store import capital_overview
        from workflows.weekly_scan import load_weekly_universe

        # Get swing capital from unified pool
        overview = capital_overview()
        swing_capital = overview.get("swing_capital_available", 1500.0)

        # Build swing universe: S&P500+Nasdaq100 prefiltered to ~150 liquid movers
        # This runs on the full index universe, not just the 58-stock weekly list
        from core.screener import swing_universe_prefilter
        swing_universe = swing_universe_prefilter(max_tickers=150)
        logger.info(f"Swing universe after prefilter: {len(swing_universe)} stocks")

        # Pull ETF returns from weekly scan for RS signal (sector momentum context)
        weekly = load_weekly_universe()
        if weekly:
            micro_scores = weekly.get("sector_scores", {}).get("micro", {})
            etf_returns = {
                name: s.get("return_60d", 0.0)
                for name, s in micro_scores.items()
            }
        else:
            etf_returns = {}

        swing_signals = swing_momentum_scan(
            swing_universe,
            sector_etf_returns=etf_returns,
            regime=health["regime"],
            total_swing_capital=swing_capital,
            min_signals=2,
        )
        logger.info(f"Swing scan: {len(swing_signals)} candidates "
                    f"({sum(1 for s in swing_signals if s.conviction=='HIGH')} HIGH conviction)")

        # Growth Hunter: small/mid-cap Rule-of-40 candidates — a different
        # universe and scoring philosophy from the 7-signal screener, but
        # merged into the SAME swing_signals list so both flow through one
        # portfolio, one chart-confirmation step, and one set of entry gates.
        # Previously ran as workflows/growth_scan.py, its own schedule, and
        # called execute_buy() directly — bypassing every gate below.
        try:
            from core.screener import growth_hunter_candidates
            gh_signals = growth_hunter_candidates()
            if gh_signals:
                swing_signals.extend(gh_signals)
                logger.info(f"Growth Hunter: {len(gh_signals)} candidates merged into swing pipeline")
        except Exception as e:
            logger.warning(f"Growth Hunter scan skipped: {e}")

        # Monitor open swing positions for exit signals
        exit_alerts = monitor_open_swings()

    except Exception as e:
        logger.warning(f"Swing scan skipped: {e}")

    # Upgrade 7: Macro pulse for daily scan output
    macro_pulse_dict = {}
    try:
        from core.macro_pulse import get_macro_pulse
        macro = get_macro_pulse()
        macro_pulse_dict = {
            "ten_year_yield": macro.ten_year_yield,
            "yield_trend": macro.yield_trend,
            "dxy_trend": macro.dxy_trend,
            "credit_spread_signal": macro.credit_spread_signal,
            "headwinds": macro.headwinds,
            "headwind_count": macro.headwind_count,
            "conviction_penalty": macro.conviction_penalty,
            "note": macro.note,
        }
        if macro.headwind_count >= 2:
            logger.warning(f"[MacroPulse] {macro.headwind_count} active headwinds: {macro.note}")
    except Exception as e:
        logger.debug(f"[MacroPulse] Daily scan output skipped: {e}")

    # Save results
    output = {
        "timestamp": datetime.datetime.now().isoformat(),
        "regime": health["regime"],
        "vix": health["vix"],
        "new_buys_paused": health.get("new_buys_paused", False),
        "vix_spike_actions": vix_spike_alerts,
        "total_scanned": len(results),
        "buy_signals": len([r for r in results if r.signal == "BUY"]),
        "watchlist_signals": len([r for r in results if r.signal == "WATCHLIST"]),
        "results": [_result_to_dict(r) for r in results],
        "swing_candidates": [_swing_to_dict(s) for s in swing_signals],
        "swing_exit_alerts": [_exit_to_dict(a) for a in exit_alerts],
        "macro_pulse": macro_pulse_dict,
    }
    _save_report(output, "daily_scan")

    # Also save swing candidates as standalone file so /api/swing/candidates picks them up
    if swing_signals:
        swing_output = {
            "date": datetime.date.today().isoformat(),
            "regime": health["regime"],
            "candidates": [_swing_to_dict(s) for s in swing_signals],
            "exit_alerts": [_exit_to_dict(a) for a in exit_alerts],
        }
        _save_report(swing_output, "swing_candidates")

    buy_tickers = [r.ticker for r in results if r.signal == "BUY"]
    logger.info(f"Scan complete. BUY signals: {buy_tickers}")

    # Log all BUY signals for outcome tracking
    auto_log_scan_signals(results)

    # Upgrade 5: Record signals in signal_tracker for adaptive weight feedback
    try:
        from core.signal_tracker import record_signal
        buy_results = [r for r in results if r.signal == "BUY"]
        for r in buy_results:
            record_signal(r)
        if buy_results:
            logger.info(f"[SignalTracker] Recorded {len(buy_results)} BUY signals")
    except Exception as e:
        logger.debug(f"[SignalTracker] record_signal skipped: {e}")

    # Paper trading: run daily monitor (auto-exit stops + profit trims) THEN auto-enter new BUYs
    try:
        from core.paper_trading import daily_update, auto_execute_scan_signals
        monitor = daily_update()
        if monitor.get("actions_taken"):
            logger.info(f"Paper trading monitor: {monitor['actions_taken']}")
        paper_trades = auto_execute_scan_signals()
        if paper_trades:
            logger.info(f"Paper trading: opened {len(paper_trades)} new positions")
    except Exception as e:
        logger.warning(f"Paper trading error: {e}")

    # Growth/swing paper portfolio: update stops + exits, then auto-enter validated setups
    try:
        from core.growth_paper_trading import (auto_enter_swing_signals,
                                               daily_update as growth_daily_update,
                                               execute_exit_alerts)
        growth_alerts = growth_daily_update()
        if growth_alerts:
            logger.info(f"Growth portfolio: {growth_alerts}")
        # Momentum-stall / thesis-break alerts were previously dashboard-only;
        # the philosophy says paper exits are fully automated — so execute them.
        if exit_alerts:
            auto_exits = execute_exit_alerts(exit_alerts)
            if auto_exits:
                logger.info(f"Growth auto-exits: {auto_exits}")
        if swing_signals and not health.get("new_buys_paused"):
            swing_entries = auto_enter_swing_signals(swing_signals)
            if swing_entries:
                logger.info(f"Swing auto-entries: {len(swing_entries)}")
    except Exception as e:
        logger.warning(f"Growth portfolio update error: {e}")

    # Upgrade 2: Dynamic stop re-evaluation for paper portfolio positions
    try:
        from core.stop_loss import reevaluate_stop
        from core.paper_trading import load_paper_portfolio, save_paper_portfolio

        pp = load_paper_portfolio()
        stop_updates = []
        for pos_ticker, pos in pp.positions.items():
            # PaperPosition is a dataclass — access attributes directly
            current_stop = getattr(pos, "stop_tier3", None) or getattr(pos, "stop_tier1", None)
            entry_price = getattr(pos, "entry_price", None)
            if current_stop and entry_price:
                re = reevaluate_stop(pos_ticker, float(current_stop), float(entry_price))
                if re["updated"]:
                    pos.stop_tier3 = re["new_stop"]
                    stop_updates.append(f"{pos_ticker}: stop {re['old_stop']:.2f}→{re['new_stop']:.2f}")
                    logger.info(f"[StopReeval] {re['reason']}")
        if stop_updates:
            save_paper_portfolio(pp)
            logger.info(f"[StopReeval] Updated {len(stop_updates)} paper stops: {', '.join(stop_updates)}")
    except Exception as e:
        logger.debug(f"[StopReeval] Paper portfolio stop re-eval skipped: {e}")

    try:
        from core.growth_paper_trading import load_growth_portfolio, _save_growth_portfolio
        gp = load_growth_portfolio()
        growth_stop_updates = []
        if hasattr(gp, "positions") and gp.positions:
            # Positions are GrowthPosition dataclasses — the old isinstance(pos, dict)
            # guard skipped every one of them, so this re-eval never ran.
            for pos_ticker, pos in gp.positions.items():
                current_stop = getattr(pos, "stop_price", None)
                entry_price = getattr(pos, "entry_price", None)
                if current_stop and entry_price:
                    re = reevaluate_stop(pos_ticker, float(current_stop), float(entry_price))
                    if re["updated"]:
                        pos.stop_price = re["new_stop"]
                        growth_stop_updates.append(f"{pos_ticker}: stop {re['old_stop']:.2f}→{re['new_stop']:.2f}")
                        logger.info(f"[StopReeval] Growth {re['reason']}")
        if growth_stop_updates:
            _save_growth_portfolio(gp)
            logger.info(f"[StopReeval] Updated {len(growth_stop_updates)} growth stops")
    except Exception as e:
        logger.debug(f"[StopReeval] Growth portfolio stop re-eval skipped: {e}")

    # Upgrade 6: Chart vision refresh for stale swing candidates
    try:
        from core.chart_refresh import refresh_stale_candidates
        chart_changes = refresh_stale_candidates(max_age_days=3)
        if chart_changes:
            logger.info(f"[ChartRefresh] {len(chart_changes)} candidates updated: "
                        f"{[c['ticker'] + ':' + c['action'] for c in chart_changes]}")
    except Exception as e:
        logger.debug(f"[ChartRefresh] Skipped: {e}")

    # GAP 17: behavioral bias sweep after each scan
    bias_report = scan_for_biases()
    if not bias_report.clean:
        logger.warning("Behavioral bias flags detected — review before acting on signals")

    # GAP 16: baseline comparison (only if we have enough signal history)
    try:
        backtest = run_signal_replay(config=BacktestConfig(), save_report=False)
        if backtest.total_signals >= 5:
            compare_to_baseline(backtest)
    except Exception as e:
        logger.debug(f"Baseline comparison skipped: {e}")

    return results


def _ticker_to_microsector(ticker: str) -> str:
    from core.sector_map import MICRO_SECTORS
    for ms_name, ms_data in MICRO_SECTORS.items():
        if ticker in ms_data.get("stocks", []):
            return ms_name
    return "wildcard"


def _build_portfolio_state():
    """PortfolioState from the paper portfolio, so scan-time gates see real holdings."""
    from agents.portfolio_manager import PortfolioState, Position
    from core.paper_trading import load_paper_portfolio

    pp = load_paper_portfolio()
    total = pp.total_value or 1.0
    positions = {}
    for t, p in pp.positions.items():
        sector = getattr(p, "sector", "unknown")
        if sector in ("unknown", "open_position", ""):
            sector = _ticker_to_microsector(t)
        positions[t] = Position(
            ticker=t,
            entry_price=p.entry_price,
            current_price=p.current_price,
            shares=p.shares,
            conviction=p.conviction,
            sector=sector,
            atr=p.atr_at_entry or 0.0,
            peak_price=p.peak_price or p.current_price,
        )
    return PortfolioState(
        positions=positions,
        cash_pct=pp.cash / total,
        portfolio_value=total,
    )


def _swing_to_dict(s) -> dict:
    return {
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
        # Chart analysis fields (None if not analyzed yet)
        "entry_type":          getattr(s, "entry_type", None),
        "pattern":             getattr(s, "pattern", None),
        "pattern_confidence":  getattr(s, "pattern_confidence", None),
        "entry_zone_low":      getattr(s, "entry_zone_low", None),
        "entry_zone_high":     getattr(s, "entry_zone_high", None),
        "stop_level":          getattr(s, "stop_level", None),
        "target_level":        getattr(s, "target_level", None),
        "risk_reward":         getattr(s, "risk_reward", None),
        "support_levels":      getattr(s, "support_levels", None),
        "resistance_levels":   getattr(s, "resistance_levels", None),
        "chart_thesis":        getattr(s, "chart_thesis", None),
        "chart_path":          getattr(s, "chart_path", None),
    }


def _exit_to_dict(a) -> dict:
    return {
        "ticker": a.ticker,
        "reason": a.reason,
        "urgency": a.urgency,
        "current_price": a.current_price,
        "entry_price": a.entry_price,
        "return_pct": a.return_pct,
        "detail": a.detail,
        "action": a.action,
    }


def _result_to_dict(r: AnalysisResult) -> dict:
    lt = getattr(r, "lt_chart", None)
    lt_dict = None
    if lt is not None:
        lt_dict = {
            "entry_type":         lt.entry_type,
            "pattern":            lt.pattern,
            "pattern_confidence": lt.pattern_confidence,
            "entry_zone_low":     lt.entry_zone_low,
            "entry_zone_high":    lt.entry_zone_high,
            "stop_level":         lt.stop_level,
            "target_level":       lt.target_level,
            "risk_reward":        lt.risk_reward,
            "support_levels":     lt.support_levels,
            "resistance_levels":  lt.resistance_levels,
            "chart_thesis":       lt.chart_thesis,
            "chart_path":         lt.chart_path,
        }
    return {
        "ticker": r.ticker,
        "lt_chart": lt_dict,
        "signal": r.signal,
        "conviction": r.conviction,
        "data_confidence": r.data_confidence,
        "hunter_score": r.hunter_score,
        "sentiment_boost": r.sentiment_boost,
        "data_quality": r.data_quality,
        "sector": r.sector,
        "sector_status": r.sector_status,
        "regime": r.regime,
        "stop_tier1": r.stop_tier1,
        "stop_tier2": r.stop_tier2,
        "stop_tier3": r.stop_tier3,
        "earnings_phase": r.earnings_phase,
        "days_to_earnings": r.days_to_earnings,
        "recommended_pct": r.recommended_position_pct,
        "red_flags": r.red_flags,
        "kill_switch": r.kill_switch,
        "alerts": r.alerts,
        "thesis": r.thesis,
        "requires_human_review": r.requires_human_review,
    }


def _save_report(data: dict, name: str) -> None:
    os.makedirs("data", exist_ok=True)
    date_str = datetime.date.today().isoformat()
    path = os.path.join("data", f"{name}_{date_str}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    logger.debug(f"Saved {name} to {path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = run_daily_scan()
    for r in results[:10]:
        print(f"{r.signal:9} | {r.ticker:5} | C={r.conviction:.1f} | {r.thesis[:80]}")
