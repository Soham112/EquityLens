"""
Sampled Chart-Vision Backtest (E13) — grades the analyst itself.

Takes a stratified sample of historical screener candidates (from E11's
strategy backtest), renders each chart AS OF that date with the exact
production renderer (incl. MA200 + 52w-high context), runs the exact
production vision prompt, then simulates outcomes with the production exit
engine (E12 S1-only stops). Compares:

    vision says ENTER (breakout/pullback/bounce, in zone)  vs
    vision says WAIT                                        vs
    everyone (enter every candidate blindly)

If vision's ENTER cohort doesn't beat blind entry, the $0.014/chart is
buying theater, not alpha. Budget: ONE-TIME user-approved ~$5 (~350 charts).
Never wire this into daily scans — see memory: api spend approvals are
one-time (user, 2026-07-09).

Incremental: results append to data/vision_backtest.jsonl after EVERY chart,
so an interruption loses nothing already paid for. Re-running skips done ones.
"""
import datetime
import json
import logging
import os
import random
import time

logger = logging.getLogger(__name__)

RESULTS_FILE = "data/vision_backtest.jsonl"
MAX_CHARTS = 350
COST_GUARD_USD = 5.0
# Measured, not estimated: the 2026-07-09 run billed ~$10 for ~350 charts
# (my prior 0.014 estimate was half the real cost — larger image tokens)
EST_COST_PER_CHART = 0.030


def _sample_entries(n=MAX_CHARTS):
    """Stratified sample from E11 candidates: half 2022-23, half 2024-26."""
    from core.strategy_backtest import screener_backtest
    _, entries, raw = screener_backtest()
    e1 = [e for e in entries if e[3] < "2024-01-01"]
    e2 = [e for e in entries if e[3] >= "2024-01-01"]
    random.seed(13)
    k = n // 2
    sample = random.sample(e1, min(k, len(e1))) + random.sample(e2, min(n - k, len(e2)))
    random.shuffle(sample)
    return sample, raw


def _done_keys():
    if not os.path.exists(RESULTS_FILE):
        return set()
    keys = set()
    with open(RESULTS_FILE) as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                keys.add((d["ticker"], d["date"]))
    return keys


def run_vision_backtest(max_charts: int = MAX_CHARTS) -> None:
    import pandas as pd
    from core.swing_chart_analysis import (_find_sr_levels, render_swing_chart,
                                           _run_vision_analysis, _calc_atr,
                                           LOOKBACK_DAYS)
    from core.strategy_backtest import _simulate, _s1_support, EXIT_VARIANTS

    sample, raw = _sample_entries(max_charts)
    done = _done_keys()
    todo = [e for e in sample if (e[0], e[3]) not in done]
    est = len(todo) * EST_COST_PER_CHART
    if est > COST_GUARD_USD:
        todo = todo[: int(COST_GUARD_USD / EST_COST_PER_CHART)]
        logger.warning(f"[VisionBT] cost guard: trimmed to {len(todo)} charts (~${COST_GUARD_USD})")
    logger.info(f"[VisionBT] {len(done)} done, {len(todo)} to analyze (~${len(todo)*EST_COST_PER_CHART:.2f})")

    # E12 production exit config: S1-only primary, 2.5xATR fallback (see _simulate)
    exit_cfg = dict(use_s1=True, atr_mult=None, trail_at=0.30, trail_pct=0.15,
                    stall=True, time_stop=None)

    for n_done, (t, i, n_sig, date_str) in enumerate(todo, 1):
        # Pre-cost steps: safe to skip-and-continue on failure (no money spent yet)
        try:
            df_full = raw[t].dropna(subset=["Close"])
            # as-of-date slice: ~400 calendar rows of context, ending at entry day
            df_asof = df_full.iloc[max(0, i - 400): i + 1]
            if len(df_asof) < 120:
                continue
            price = float(df_asof["Close"].iloc[-1])
            sr = _find_sr_levels(df_asof.tail(LOOKBACK_DAYS), price)
            png, _ = render_swing_chart(t, df_asof, sr, save=False)
            if png is None:
                continue
        except Exception as e:
            logger.warning(f"[VisionBT] {t} {date_str} pre-cost step failed (skipping): {e}")
            continue

        # THE PAID CALL — anything failing after this point ABORTS the whole run
        # (2026-07-09 incident: continuing after post-call failures burned ~$10
        # across 350 calls with zero records saved)
        verdict = _run_vision_analysis(t, png, sr, price, df=df_asof)
        if verdict is None:
            logger.error(f"[VisionBT] {t}: vision call returned None — aborting run "
                         "(check API credits/limits before resuming)")
            break
        try:
            # forward simulation with production exits (daily closes after entry)
            closes = df_full["Close"].values
            lows = df_full["Low"].values
            vols = df_full["Volume"].values
            v90s = df_full["Volume"].rolling(90).mean().values
            atr = _calc_atr(df_asof) or price * 0.02
            s1 = next((l.price for l in sr if l.label == "S1"), None)
            sim_ret, hold, why = _simulate(closes, lows, vols, v90s, i, exit_cfg, s1, atr)

            entry_type = verdict.get("entry_type", "wait")
            lo = float(verdict.get("entry_zone_low") or 0) or None
            hi = float(verdict.get("entry_zone_high") or 0) or None
            in_zone = (lo is not None and hi is not None
                       and lo * 0.98 <= price <= hi * 1.05)
            rec = {
                "ticker": str(t), "date": str(date_str), "n_signals": int(n_sig),
                "entry_type": str(entry_type), "in_zone": bool(in_zone),
                "pattern": verdict.get("pattern"), "pattern_confidence": verdict.get("pattern_confidence"),
                "vision_rr": verdict.get("risk_reward"),
                "sim_return": round(float(sim_ret), 4), "hold_days": int(hold), "exit_reason": str(why),
                "fwd21": round(float(closes[i + 21] / price - 1), 4) if i + 21 < len(closes) else None,
            }
            # default=float catches any residual numpy scalar — a paid vision call
            # must NEVER be lost to a serialization error again (2026-07-09: 350
            # calls, ~$10, zero records saved because n_signals was numpy int64)
            with open(RESULTS_FILE, "a") as f:
                f.write(json.dumps(rec, default=float) + "\n")
            # Fail-fast proof-of-save: the first paid call must produce a readable
            # record on disk before any further money is spent
            if n_done == 1:
                with open(RESULTS_FILE) as f:
                    assert any(line.strip() for line in f), "first record did not persist"
            logger.info(f"[VisionBT] {n_done}/{len(todo)} {t} {date_str}: "
                        f"{entry_type} {rec['pattern']} → sim {sim_ret:+.1%} ({why})")
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"[VisionBT] {t} {date_str} FAILED AFTER PAID CALL — "
                         f"ABORTING to stop spend: {e}")
            break
    logger.info("[VisionBT] run complete")


def report() -> dict:
    """Cohort comparison: vision-ENTER vs vision-WAIT vs everyone."""
    import numpy as np
    recs = []
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            recs = [json.loads(l) for l in f if l.strip()]
    if not recs:
        return {"charts": 0}

    def stats(rs):
        if not rs:
            return {"n": 0}
        sim = [r["sim_return"] for r in rs]
        return {"n": len(rs), "avg_sim": round(float(np.mean(sim)), 4),
                "median_sim": round(float(np.median(sim)), 4),
                "win_rate": round(float(np.mean([x > 0 for x in sim])), 3)}

    actionable = [r for r in recs if r["entry_type"] in ("breakout", "pullback", "bounce")]
    enter = [r for r in actionable if r["in_zone"]]
    out = {
        "charts": len(recs),
        "everyone_blind": stats(recs),
        "vision_enter_in_zone": stats(enter),
        "vision_actionable_any_zone": stats(actionable),
        "vision_wait": stats([r for r in recs if r["entry_type"] == "wait"]),
        "by_entry_type": {et: stats([r for r in recs if r["entry_type"] == et])
                          for et in ("breakout", "pullback", "bounce", "wait")},
        "by_pattern": {p: stats([r for r in recs if r["pattern"] == p])
                       for p in sorted({r["pattern"] for r in recs if r.get("pattern")})
                       if sum(1 for r in recs if r["pattern"] == p) >= 8},
        "by_era": {era: {"enter": stats([r for r in enter if cond(r["date"])]),
                         "wait": stats([r for r in recs if r["entry_type"] == "wait" and cond(r["date"])])}
                   for era, cond in [("2022-2023", lambda d: d < "2024-01-01"),
                                     ("2024-2026", lambda d: d >= "2024-01-01")]},
    }
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_vision_backtest()
    print(json.dumps(report(), indent=1))
