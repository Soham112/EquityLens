"""
Signal Tracker / Feedback Loop — records BUY/WATCHLIST signals and tracks outcomes.
Enables adaptive weight adjustments in Hunter based on 90-day signal history.

Data stored in: data/signal_outcomes.jsonl (one JSON object per line)
"""
import datetime
import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger(__name__)

OUTCOMES_FILE = "data/signal_outcomes.jsonl"
HIT_THRESHOLD = 0.10     # +10% = hit
MISS_THRESHOLD = -0.10   # -10% = miss
LOOKBACK_DAYS = 90       # outcome window


@dataclass
class SignalOutcome:
    ticker: str
    signal_date: str
    signal: str             # BUY | WATCHLIST
    conviction: float
    entry_price: float
    price_30d: Optional[float]
    price_90d: Optional[float]
    return_30d: Optional[float]
    return_90d: Optional[float]
    hit: Optional[bool]     # True if the SIMULATED TRADE returned >= +10% — not "touched +10%"
    hunter_score: float
    sentiment_boost: float
    sector: str
    entry_type: Optional[str]   # from chart analysis: "breakout" | "pullback" | "bounce" | None
    # Trade-consistent scoring: outcomes are simulated with the same stop the
    # portfolio would trade, so the feedback loop learns from tradeable returns.
    stop_price: Optional[float] = None    # tier-3 hard stop at signal time
    exit_reason: Optional[str] = None     # "stop" | "held" | None while pending
    sim_return: Optional[float] = None    # return at stop-out or at the 90d mark
    # E8 shadow tracking: which gate(s) demoted this signal from BUY → WATCHLIST
    # (valuation_cap, macro_penalty, sector_gate, ...). None = organic signal.
    # The 30/90d scoring above turns every demotion into a "shadow trade" — we
    # measure what the blocked stock did after we passed on it.
    demoted_by: Optional[list] = None
    # E10: how the ticker got into the scan universe. None = sector funnel;
    # "radar" = accel-radar probe candidate (audition — same gates apply)
    source: Optional[str] = None


def _load_outcomes() -> list[SignalOutcome]:
    if not os.path.exists(OUTCOMES_FILE):
        return []
    outcomes = []
    try:
        with open(OUTCOMES_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    outcomes.append(SignalOutcome(**d))
    except Exception as e:
        logger.warning(f"[SignalTracker] Could not load outcomes: {e}")
    return outcomes


def _save_outcomes(outcomes: list[SignalOutcome]) -> None:
    os.makedirs("data", exist_ok=True)
    with open(OUTCOMES_FILE, "w") as f:
        for o in outcomes:
            f.write(json.dumps(asdict(o)) + "\n")


def record_signal(result, chart_signal=None, source: Optional[str] = None) -> None:
    """
    Record a BUY or WATCHLIST signal for outcome tracking.
    result: AnalysisResult from orchestrator
    chart_signal: optional SwingChartSignal or similar with .entry_type
    source: "radar" when the ticker entered the universe via the accel-radar probe
    """
    if result.signal not in ("BUY", "WATCHLIST"):
        return

    # Avoid duplicate entries for same ticker+date
    outcomes = _load_outcomes()
    today = datetime.date.today().isoformat()
    for o in outcomes:
        if o.ticker == result.ticker and o.signal_date == today:
            return  # already recorded today

    entry_type = None
    if chart_signal:
        entry_type = getattr(chart_signal, "entry_type", None)
    elif result.lt_chart:
        entry_type = getattr(result.lt_chart, "entry_type", None)

    outcome = SignalOutcome(
        ticker=result.ticker,
        signal_date=today,
        signal=result.signal,
        conviction=result.conviction,
        entry_price=0.0,   # real price only — a 0 record is excluded from scoring,
                           # a stop-price proxy silently corrupts every downstream stat
        price_30d=None,
        price_90d=None,
        return_30d=None,
        return_90d=None,
        hit=None,
        hunter_score=result.hunter_score,
        sentiment_boost=result.sentiment_boost,
        sector=result.sector,
        entry_type=entry_type,
        stop_price=result.stop_tier3,
        demoted_by=getattr(result, "demoted_by", None),
        source=source,
    )

    try:
        import yfinance as yf
        info = yf.Ticker(result.ticker).fast_info
        price = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
        if price and price > 0:
            outcome.entry_price = round(float(price), 4)
    except Exception:
        pass
    if outcome.entry_price <= 0:
        logger.warning(f"[SignalTracker] {result.ticker}: no entry price available — "
                       "recorded but excluded from outcome scoring")

    outcomes.append(outcome)
    _save_outcomes(outcomes)
    logger.debug(f"[SignalTracker] Recorded {result.signal} signal for {result.ticker}")


def _split_factors(tickers: list[str]) -> dict[str, list[tuple[datetime.date, float]]]:
    """Split events per ticker — needed to compare raw-at-signal-time entry/stop
    prices against split-adjusted forward closes."""
    factors: dict[str, list[tuple[datetime.date, float]]] = {}
    try:
        import yfinance as yf
        for t in tickers:
            try:
                s = yf.Ticker(t).splits
                if s is not None and len(s) > 0:
                    factors[t] = [(dt.date(), float(r)) for dt, r in s.items() if r and r > 0]
            except Exception:
                continue
    except ImportError:
        pass
    return factors


def _factor_since(events: list[tuple[datetime.date, float]], since: datetime.date) -> float:
    f = 1.0
    for dt, ratio in events:
        if dt > since:
            f *= ratio
    return f


def update_outcomes() -> int:
    """
    Score pending outcomes (hit=None) by SIMULATING the trade with the stop
    recorded at signal time — the old "touched +10% anytime in 90d" definition
    counted spike-and-crash names as wins and ignored stop-outs entirely,
    which trained the adaptive weights on returns nobody could have traded.

    Simulation per signal:
      - walk daily closes from the signal date
      - close <= stop_price  → trade over: hit=False, exit_reason="stop",
        sim_return = the stop-out return (scored immediately, not at 90d)
      - survives ~63 trading days (90 calendar) → exit_reason="held",
        sim_return = return at day 63, hit = sim_return >= +10%

    One batch download for all pending tickers (yfinance rate limits).
    Returns number of outcomes newly completed.
    """
    outcomes = _load_outcomes()
    pending = [o for o in outcomes if o.hit is None and o.entry_price > 0]
    if not pending:
        return 0

    try:
        import pandas as pd
        import yfinance as yf
    except ImportError:
        logger.warning("[SignalTracker] yfinance not available")
        return 0

    today = datetime.date.today()
    tickers = sorted({o.ticker for o in pending})
    earliest = min(o.signal_date for o in pending)
    try:
        raw = yf.download(tickers, start=earliest, progress=False,
                          auto_adjust=True, group_by="ticker")
    except Exception as e:
        logger.warning(f"[SignalTracker] batch download failed: {e}")
        return 0
    if raw is None or raw.empty:
        return 0
    multi = isinstance(raw.columns, pd.MultiIndex)
    available = set(raw.columns.get_level_values(0)) if multi else None
    splits = _split_factors(tickers)

    updated = 0
    for o in pending:
        try:
            if multi:
                if o.ticker not in available:
                    continue
                closes = raw[o.ticker]["Close"].dropna()
            else:
                closes = raw["Close"].dropna()
            # Bars strictly after the signal date — the signal-day close is the entry
            signal_dt = datetime.date.fromisoformat(o.signal_date)
            closes = closes[closes.index.date > signal_dt]
            if closes.empty:
                continue

            # Entry/stop were recorded RAW at signal time; forward closes are
            # split-adjusted. Rescale by any splits since the signal so a 4:1
            # split doesn't read as a -75% crash into the stop.
            factor = _factor_since(splits.get(o.ticker, []), signal_dt)
            entry = o.entry_price / factor
            stop = (o.stop_price / factor) if o.stop_price else None
            if stop and stop >= entry:
                logger.warning(f"[SignalTracker] {o.ticker} {o.signal_date}: stop "
                               f"{stop:.2f} >= entry {entry:.2f} after split adj — stop ignored")
                stop = None

            days_since = (today - signal_dt).days

            # 30-day checkpoint (~21 trading days) — informational
            if days_since >= 30 and len(closes) >= 21:
                p30 = float(closes.iloc[20])
                o.price_30d = round(p30, 4)
                o.return_30d = round((p30 - entry) / entry, 4)

            # Stop simulation: first close at/below the stop ends the trade
            if stop:
                stopped = closes[closes <= stop]
                if not stopped.empty:
                    exit_price = float(stopped.iloc[0])
                    o.sim_return = round((exit_price - entry) / entry, 4)
                    o.exit_reason = "stop"
                    o.hit = False
                    updated += 1
                    continue

            # Survived to the 90-day mark (~63 trading days)
            if days_since >= 90 and len(closes) >= 63:
                p90 = float(closes.iloc[62])
                o.price_90d = round(p90, 4)
                o.return_90d = round((p90 - entry) / entry, 4)
                o.sim_return = o.return_90d
                o.exit_reason = "held"
                o.hit = o.sim_return >= HIT_THRESHOLD
                updated += 1

        except Exception as e:
            logger.debug(f"[SignalTracker] Could not update {o.ticker}: {e}")

    _save_outcomes(outcomes)
    logger.info(f"[SignalTracker] Updated {updated} outcomes")
    return updated


def get_performance_stats() -> dict:
    """
    Compute performance statistics across all completed signal outcomes.
    """
    outcomes = _load_outcomes()
    completed = [o for o in outcomes if o.hit is not None]

    if not completed:
        return {
            "total_signals": 0,
            "buy_hit_rate": None,
            "watchlist_hit_rate": None,
            "best_sector": None,
            "worst_sector": None,
            "best_entry_type": None,
            "conviction_accuracy": None,
            "weight_suggestions": {},
            "note": "No completed outcomes yet (need 90+ days of data)",
        }

    buy_outcomes = [o for o in completed if o.signal == "BUY"]
    wl_outcomes = [o for o in completed if o.signal == "WATCHLIST"]

    buy_hit_rate = (sum(1 for o in buy_outcomes if o.hit) / len(buy_outcomes)) if buy_outcomes else None
    wl_hit_rate = (sum(1 for o in wl_outcomes if o.hit) / len(wl_outcomes)) if wl_outcomes else None

    # Sector performance
    sector_results: dict[str, list[bool]] = {}
    for o in completed:
        sector_results.setdefault(o.sector, []).append(bool(o.hit))

    sector_hit_rates = {s: sum(hits) / len(hits) for s, hits in sector_results.items() if len(hits) >= 3}
    best_sector = max(sector_hit_rates, key=sector_hit_rates.get) if sector_hit_rates else None
    worst_sector = min(sector_hit_rates, key=sector_hit_rates.get) if sector_hit_rates else None

    # Entry type performance
    entry_results: dict[str, list[bool]] = {}
    for o in completed:
        if o.entry_type:
            entry_results.setdefault(o.entry_type, []).append(bool(o.hit))

    entry_hit_rates = {e: sum(hits) / len(hits) for e, hits in entry_results.items() if len(hits) >= 3}
    best_entry_type = max(entry_hit_rates, key=entry_hit_rates.get) if entry_hit_rates else None

    # Conviction accuracy (Pearson correlation between conviction and return_90d)
    conv_return_pairs = [
        (o.conviction, o.return_90d)
        for o in completed
        if o.return_90d is not None
    ]
    conviction_accuracy = None
    if len(conv_return_pairs) >= 5:
        n = len(conv_return_pairs)
        convs = [p[0] for p in conv_return_pairs]
        rets = [p[1] for p in conv_return_pairs]
        mean_c = sum(convs) / n
        mean_r = sum(rets) / n
        cov = sum((c - mean_c) * (r - mean_r) for c, r in zip(convs, rets))
        var_c = sum((c - mean_c) ** 2 for c in convs)
        var_r = sum((r - mean_r) ** 2 for r in rets)
        if var_c > 0 and var_r > 0:
            conviction_accuracy = round(cov / math.sqrt(var_c * var_r), 3)

    # Weight suggestions
    weight_suggestions = {}
    # We track which dimension correlates with hits by using hunter_score as proxy for fundamentals
    fund_high = [o for o in completed if o.hunter_score >= 7]
    fund_hit = sum(1 for o in fund_high if o.hit) / len(fund_high) if fund_high else 0.5
    if fund_hit > 0.70:
        weight_suggestions["fundamentals"] = "increase weight (+0.05) — high accuracy when hunter_score >= 7"
    tech_high = [o for o in completed if o.entry_type in ("breakout", "pullback")]
    tech_hit = sum(1 for o in tech_high if o.hit) / len(tech_high) if tech_high else 0.5
    if tech_hit > 0.70:
        weight_suggestions["technicals"] = "increase weight (+0.05) — breakout/pullback entries outperforming"

    return {
        "total_signals": len(completed),
        "buy_hit_rate": round(buy_hit_rate, 3) if buy_hit_rate is not None else None,
        "watchlist_hit_rate": round(wl_hit_rate, 3) if wl_hit_rate is not None else None,
        "best_sector": best_sector,
        "worst_sector": worst_sector,
        "best_entry_type": best_entry_type,
        "conviction_accuracy": conviction_accuracy,
        "weight_suggestions": weight_suggestions,
        "sector_hit_rates": {s: round(r, 3) for s, r in sector_hit_rates.items()},
        "entry_type_hit_rates": {e: round(r, 3) for e, r in entry_hit_rates.items()},
    }


def get_adaptive_weights() -> dict:
    """
    Returns adjusted weights for Hunter based on 90-day performance history.
    Base: fundamentals=0.50, technicals=0.30, valuation=0.20
    """
    BASE = {"fundamentals": 0.50, "technicals": 0.30, "valuation": 0.20}
    MIN_W, MAX_W = 0.10, 0.65

    outcomes = _load_outcomes()
    completed = [o for o in outcomes if o.hit is not None]

    if len(completed) < 10:
        return BASE  # not enough data to adapt

    weights = dict(BASE)

    # Symmetric adjustment: a dimension earns weight above 70% hit rate and
    # LOSES weight below 40% — the old ratchet only ever rewarded, so a
    # persistently failing dimension kept its full base weight forever.
    def _shift(target: str, others: dict[str, float], hit_rate: float):
        if hit_rate > 0.70:
            delta = 0.05
        elif hit_rate < 0.40:
            delta = -0.05
        else:
            return
        weights[target] = max(MIN_W, min(MAX_W, weights[target] + delta))
        for name, share in others.items():
            weights[name] = max(MIN_W, min(MAX_W, weights[name] - delta * share))

    # Fundamentals signal: high hunter_score outcomes
    fund_high = [o for o in completed if o.hunter_score >= 7]
    if fund_high and len(fund_high) >= 5:
        fund_hit_rate = sum(1 for o in fund_high if o.hit) / len(fund_high)
        _shift("fundamentals", {"technicals": 0.6, "valuation": 0.4}, fund_hit_rate)

    # Technicals signal: breakout/pullback entry types
    tech_entries = [o for o in completed if o.entry_type in ("breakout", "pullback")]
    if tech_entries and len(tech_entries) >= 5:
        tech_hit_rate = sum(1 for o in tech_entries if o.hit) / len(tech_entries)
        _shift("technicals", {"fundamentals": 0.7, "valuation": 0.3}, tech_hit_rate)

    # Normalize to sum = 1.0
    total = sum(weights.values())
    weights = {k: round(v / total, 3) for k, v in weights.items()}

    logger.debug(f"[SignalTracker] Adaptive weights: {weights} (base: {BASE})")
    return weights


# ── E8: Shadow gate tracking ──────────────────────────────────────────────────
# Every gate-demoted signal is a "shadow trade": we record what the blocked
# stock did over 30/90d and compare each gate's cohort against the BUYs we
# actually entered. A gate whose blocked stocks consistently BEAT our entries
# is costing money; one whose blocked stocks lag is earning its keep.
# Pre-registered judgment (EXPERIMENTS.md E8): min 15 scored signals per cohort,
# compare avg 90d return vs entered BUYs. Evidence-surfacing only — recalibrating
# a gate stays a deliberate decision, logged as its own experiment entry.

SHADOW_MIN_SCORED = 15


def shadow_gate_report() -> dict:
    """Per-gate shadow cohorts vs entered BUYs. Powers /api/feedback/shadow."""
    outcomes = [o for o in _load_outcomes() if o.entry_price and o.entry_price > 0]

    def stats(cohort: list) -> dict:
        r30 = [o.return_30d for o in cohort if o.return_30d is not None]
        r90 = [o.return_90d for o in cohort if o.return_90d is not None]
        hits = [o.hit for o in cohort if o.hit is not None]
        return {
            "signals": len(cohort),
            "scored_30d": len(r30),
            "scored_90d": len(r90),
            "avg_return_30d": round(sum(r30) / len(r30), 4) if r30 else None,
            "avg_return_90d": round(sum(r90) / len(r90), 4) if r90 else None,
            "hit_rate": round(sum(hits) / len(hits), 3) if hits else None,
            "tickers": [o.ticker for o in cohort[-8:]],   # most recent few, for the dashboard
        }

    entered = [o for o in outcomes if o.signal == "BUY" and not o.demoted_by]
    report = {"entered_buys": stats(entered)}

    gates: dict[str, list] = {}
    for o in outcomes:
        for g in (o.demoted_by or []):
            gates.setdefault(g, []).append(o)
    for g, cohort in sorted(gates.items()):
        report[g] = stats(cohort)

    report["near_miss_conviction"] = stats(
        [o for o in outcomes
         if o.signal == "WATCHLIST" and not o.demoted_by and o.conviction >= 7.0])

    # E10: radar-probe cohort — did accel-radar-sourced candidates earn their audition?
    report["radar_sourced"] = stats([o for o in outcomes if o.source == "radar"])

    # Verdict lines once a cohort clears the pre-registered threshold
    verdicts = []
    base = report["entered_buys"]["avg_return_90d"]
    for name, c in report.items():
        if name == "entered_buys" or not isinstance(c, dict):
            continue
        if c["scored_90d"] >= SHADOW_MIN_SCORED and base is not None and c["avg_return_90d"] is not None:
            diff = c["avg_return_90d"] - base
            if diff > 0.02:
                verdicts.append(f"{name}: blocked stocks BEAT entered BUYs by {diff:+.1%} avg 90d "
                                f"({c['scored_90d']} scored) — gate may be costing money, review it")
            elif diff < -0.02:
                verdicts.append(f"{name}: blocked stocks LAG entered BUYs by {diff:+.1%} avg 90d "
                                f"({c['scored_90d']} scored) — gate is earning its keep")
            else:
                verdicts.append(f"{name}: no meaningful edge either way ({diff:+.1%} over "
                                f"{c['scored_90d']} scored)")
    report["verdicts"] = verdicts
    return report
