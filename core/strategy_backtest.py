"""
Swing Strategy Backtest (E11) — layer-by-layer historical replay.

Part A  screener_backtest(): replays the prefilter + the 3 fully-replayable
        price/volume signals weekly over ~3.5 years and measures forward
        returns by signals-fired cohort. Directly tests E7's question:
        do 3-signal candidates underperform 4-signal ones?
        (Only OHLCV-derived signals: volume_accumulation, relative_strength
        vs SPY, price_structure. catalyst/narrative/insider/squeeze are not
        honestly replayable — stated, not hidden.)

Part B  exit_backtest(): takes Part A's candidates as synthetic entries and
        replays the EXIT engine under variants: the live stop formula
        (max(S1−0.5×ATR, entry−2.5×ATR)) vs no-S1, tighter floors, S1-only,
        different trailing configs, a 21d time stop, and no-stall-exit.

Honesty notes (also embedded in output):
  - Universe = today's constituents → mild survivorship bias over 3.5y
  - Entries overlap (a trending stock re-qualifies weekly) → results grade
    RULE quality per trade, not portfolio equity
  - Daily-close simulation, same basis as the live daily_update

Outputs data/strategy_backtest.json. One batch download (CLAUDE.md rule).
"""
import datetime
import json
import logging
import os
import random
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_FILE = "data/strategy_backtest.json"
START = "2021-01-01"           # warmup for MA200 before first 2022 signal
FIRST_SIGNAL = "2022-01-01"
FWD_MAX = 63                   # 63 trading days ≈ 90 calendar (swing horizon cap)
MAX_ENTRIES_SIM = 4000         # cap Part B simulations (random sample beyond)


# ── data prep ─────────────────────────────────────────────────────────────────

def _load_prices():
    import pandas as pd
    import yfinance as yf
    from core.universe import load_universe

    uni = load_universe()
    tickers = sorted({t for t, _ in uni}) if uni else []
    if not tickers:
        raise RuntimeError("universe cache empty — run build_universe() first")
    logger.info(f"[StratBT] downloading {len(tickers)} tickers + SPY from {START}...")
    raw = yf.download(tickers + ["SPY"], start=START, progress=False,
                      auto_adjust=True, group_by="ticker", threads=True)
    return raw, tickers


def _indicators(df):
    """Per-ticker rolling indicators, all shifted to be known at the close."""
    import pandas as pd
    import numpy as np
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    out = pd.DataFrame(index=df.index)
    out["close"] = close
    out["ma50"] = close.rolling(50).mean()
    out["ma200"] = close.rolling(200).mean()
    out["ma200_3m_ago"] = out["ma200"].shift(63)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    out["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    tr = pd.concat([(high - low), (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    out["atr20"] = tr.rolling(20).mean()
    out["v20"] = vol.rolling(20).mean()
    out["v90"] = vol.rolling(90).mean()
    out["ret3m"] = close.pct_change(63)
    out["hi252"] = close.rolling(252).max()
    return out


def _s1_support(low_arr, entry_idx, entry_price, lookback=120, w=5, cluster_pct=0.015):
    """Nearest tested support below entry — replicates _find_sr_levels' lows:
    5-bar swing lows over the lookback, 1.5% clustering, >=2 tests."""
    lo = max(0, entry_idx - lookback)
    lows = low_arr[lo:entry_idx + 1]
    n = len(lows)
    raw = [lows[i] for i in range(w, n - w)
           if lows[i] == min(lows[i - w:i + w + 1])]
    if not raw:
        return None
    raw.sort()
    clusters = [[raw[0]]]
    for p in raw[1:]:
        if (p - clusters[-1][-1]) / clusters[-1][-1] < cluster_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    tested = [sum(c) / len(c) for c in clusters if len(c) >= 2]
    below = [p for p in tested if p < entry_price]
    return max(below) if below else None


# ── Part A: screener-layer replay ─────────────────────────────────────────────

def screener_backtest(raw=None, tickers=None) -> dict:
    import numpy as np
    import pandas as pd

    if raw is None:
        raw, tickers = _load_prices()
    spy = raw["SPY"]["Close"]
    spy_r60 = spy.pct_change(60)

    # weekly snapshot dates (last trading day per ISO week)
    idx = spy.index
    week_last = spy.groupby([idx.isocalendar().year, idx.isocalendar().week]).tail(1).index
    snap_dates = [d for d in week_last if d >= pd.Timestamp(FIRST_SIGNAL)
                  and d <= idx[-1] - pd.Timedelta(days=35)]

    cohorts = {k: {"fwd10": [], "fwd21": []} for k in (0, 1, 2, 3)}
    entries = []          # candidates for Part B: (ticker, date_idx_label, n_signals)
    per_signal = {s: {"fired_fwd21": [], "quiet_fwd21": []}
                  for s in ("volume_accumulation", "relative_strength", "price_structure")}

    for t in tickers:
        try:
            df = raw[t].dropna(subset=["Close"])
            if len(df) < 300:
                continue
            ind = _indicators(df)
            closes = ind["close"]
            pos = {d: i for i, d in enumerate(ind.index)}
            for d in snap_dates:
                i = pos.get(d)
                if i is None or i < 260 or i + 21 >= len(closes):
                    continue
                r = ind.iloc[i]
                if any(pd.isna(r[k]) for k in ("ma50", "ma200", "rsi", "atr20", "v20", "v90", "ret3m", "hi252")):
                    continue
                price = r["close"]
                # ── prefilter (same rules as swing_universe_prefilter) ──
                atr_pct = r["atr20"] / price
                if not (10 <= price <= 250 and r["v20"] > 1e6
                        and 35 <= r["rsi"] <= 75 and price > r["ma50"] * 0.97
                        and 0.015 <= atr_pct <= 0.06):
                    continue
                # ── the 3 replayable signals ──
                s_vol = (r["v20"] / r["v90"]) >= 1.20 if r["v90"] else False
                sr60 = spy_r60.get(d)
                s_rs = (not pd.isna(sr60)) and (r["ret3m"] - sr60) > 0.03
                stage2 = price > r["ma50"] > r["ma200"] and r["ma200"] > r["ma200_3m_ago"]
                s_ps = (price / r["hi252"] - 1) >= -0.08 and stage2
                fired = {"volume_accumulation": s_vol, "relative_strength": s_rs,
                         "price_structure": s_ps}
                n = sum(fired.values())
                f10 = float(closes.iloc[i + 10] / price - 1)
                f21 = float(closes.iloc[i + 21] / price - 1)
                cohorts[n]["fwd10"].append(f10)
                cohorts[n]["fwd21"].append(f21)
                for s, hit in fired.items():
                    per_signal[s]["fired_fwd21" if hit else "quiet_fwd21"].append(f21)
                if n >= 2:
                    entries.append((t, i, n, d.date().isoformat()))
        except Exception:
            continue

    def stats(vals):
        if not vals:
            return {"n": 0}
        return {"n": len(vals), "avg": round(float(np.mean(vals)), 4),
                "median": round(float(np.median(vals)), 4),
                "win_rate": round(float(np.mean([v > 0 for v in vals])), 3),
                "hit5_rate": round(float(np.mean([v > 0.05 for v in vals])), 3)}

    report = {
        "snapshots": len(snap_dates),
        "cohorts_by_signals_fired": {str(k): {"fwd10": stats(v["fwd10"]),
                                              "fwd21": stats(v["fwd21"])}
                                     for k, v in cohorts.items()},
        "per_signal_fwd21": {s: {"fired": stats(v["fired_fwd21"]),
                                 "quiet": stats(v["quiet_fwd21"])}
                             for s, v in per_signal.items()},
        "notes": ["signals replayed: volume_accumulation, relative_strength(vs SPY), "
                  "price_structure (Stage2+near-high branch). catalyst/narrative/"
                  "insider/squeeze not replayable — cohort counts are of 3, not 7",
                  "universe = today's constituents (mild survivorship bias)",
                  "overlapping weekly snapshots — grades signal quality, not equity"],
    }
    return report, entries, raw


# ── Part B: exit-engine replay ────────────────────────────────────────────────

EXIT_VARIANTS = {
    "live (S1-0.5ATR, 2.5ATR floor, trail@30/15, stall)": dict(use_s1=True, atr_mult=2.5, trail_at=0.30, trail_pct=0.15, stall=True, time_stop=None),
    "no S1 (pure 2.5xATR)":                                dict(use_s1=False, atr_mult=2.5, trail_at=0.30, trail_pct=0.15, stall=True, time_stop=None),
    "tighter floor (2.0xATR)":                             dict(use_s1=True, atr_mult=2.0, trail_at=0.30, trail_pct=0.15, stall=True, time_stop=None),
    "S1 only (no ATR floor)":                              dict(use_s1=True, atr_mult=None, trail_at=0.30, trail_pct=0.15, stall=True, time_stop=None),
    "earlier trail (@15%, 10% trail)":                     dict(use_s1=True, atr_mult=2.5, trail_at=0.15, trail_pct=0.10, stall=True, time_stop=None),
    "no stall exit":                                       dict(use_s1=True, atr_mult=2.5, trail_at=0.30, trail_pct=0.15, stall=False, time_stop=None),
    "21d time stop":                                       dict(use_s1=True, atr_mult=2.5, trail_at=0.30, trail_pct=0.15, stall=True, time_stop=21),
}


def _simulate(closes, lows, vols, v90s, entry_i, cfg, s1, atr):
    """Daily-close walk from entry. Returns (return_pct, hold_days, exit_reason)."""
    entry = closes[entry_i]
    stops = []
    if cfg["use_s1"] and s1 is not None:
        stops.append(s1 - 0.5 * atr)
    if cfg["atr_mult"] is not None:
        stops.append(entry - cfg["atr_mult"] * atr)
    if not stops:
        stops.append(entry - 2.5 * atr)   # S1-only with no S1 found → fallback floor
    stop = max(s for s in stops if s > 0) if any(s > 0 for s in stops) else entry * 0.75
    peak, flat_days = entry, 0
    n = len(closes)
    for k in range(1, FWD_MAX + 1):
        i = entry_i + k
        if i >= n:
            break
        px = closes[i]
        peak = max(peak, px)
        ret = px / entry - 1
        if ret >= cfg["trail_at"]:
            stop = max(stop, peak * (1 - cfg["trail_pct"]))
        if px <= stop:
            return px / entry - 1, k, "stop"
        if cfg["stall"]:
            day_move = abs(px / closes[i - 1] - 1)
            low_vol = v90s[i] and vols[i] < 0.80 * v90s[i]
            flat_days = flat_days + 1 if (low_vol and day_move < 0.015) else 0
            if flat_days >= 3:
                return px / entry - 1, k, "stall"
        if cfg["time_stop"] and k >= cfg["time_stop"]:
            return px / entry - 1, k, "time"
    k = min(FWD_MAX, n - 1 - entry_i)
    return closes[entry_i + k] / entry - 1, k, "held_63d"


def exit_backtest(entries, raw) -> dict:
    import numpy as np

    if len(entries) > MAX_ENTRIES_SIM:
        random.seed(11)
        entries = random.sample(entries, MAX_ENTRIES_SIM)

    results = {name: [] for name in EXIT_VARIANTS}
    reasons = {name: {} for name in EXIT_VARIANTS}
    arrays = {}
    for t, i, n_sig, date in entries:
        if t not in arrays:
            df = raw[t].dropna(subset=["Close"])
            arrays[t] = (df["Close"].values, df["Low"].values,
                         df["Volume"].values,
                         df["Volume"].rolling(90).mean().values,
                         df["Close"].rolling(1).mean().values)  # placeholder align
        closes, lows, vols, v90s, _ = arrays[t]
        if i + 2 >= len(closes):
            continue
        # ATR at entry (20d TR mean, computed inline to stay on raw arrays)
        seg_hi = raw[t]["High"].values[i - 20:i + 1]
        seg_lo = lows[i - 20:i + 1]
        seg_cl = closes[i - 21:i + 1]
        tr = [max(seg_hi[j] - seg_lo[j], abs(seg_hi[j] - seg_cl[j]),
                  abs(seg_lo[j] - seg_cl[j])) for j in range(len(seg_hi))]
        atr = float(np.mean(tr))
        s1 = _s1_support(lows, i, closes[i])
        for name, cfg in EXIT_VARIANTS.items():
            r, hold, why = _simulate(closes, lows, vols, v90s, i, cfg, s1, atr)
            results[name].append((r, hold, date))
            reasons[name][why] = reasons[name].get(why, 0) + 1

    def summarize(rs):
        rets = [r for r, _, _ in rs]
        holds = [h for _, h, _ in rs]
        return {
            "trades": len(rets),
            "avg_return": round(float(np.mean(rets)), 4),
            "median_return": round(float(np.median(rets)), 4),
            "win_rate": round(float(np.mean([r > 0 for r in rets])), 3),
            "avg_hold_days": round(float(np.mean(holds)), 1),
            "worst": round(float(min(rets)), 4),
        }

    report, by_era = {}, {}
    # Era-split stability check: a rule change must hold in the 2022-23 bear/chop
    # AND the 2024-26 bull separately — one regime doing all the work is the
    # equal-thirds trap (see E9 long-history lesson).
    eras = [("2022-2023", lambda d: d < "2024-01-01"),
            ("2024-2026", lambda d: d >= "2024-01-01")]
    for name, rs in results.items():
        if not rs:
            continue
        report[name] = {**summarize(rs), "exit_reasons": reasons[name]}
        by_era[name] = {era: summarize([x for x in rs if cond(x[2])])
                        for era, cond in eras
                        if any(cond(x[2]) for x in rs)}
    return report, by_era


def _r1_resistance(high_arr, entry_idx, entry_price, lookback=120, w=5, cluster_pct=0.015):
    """Nearest tested resistance ABOVE entry — exact mirror of _s1_support, using
    5-bar swing HIGHS. This is the numeric stand-in for the vision prompt's
    "first realistic target at next resistance", so the replayed R/R matches the
    live metric's intent without replaying (paid) vision over history."""
    hi = max(0, entry_idx - lookback)
    highs = high_arr[hi:entry_idx + 1]
    n = len(highs)
    raw = [highs[i] for i in range(w, n - w)
           if highs[i] == max(highs[i - w:i + w + 1])]
    if not raw:
        return None
    raw.sort()
    clusters = [[raw[0]]]
    for p in raw[1:]:
        if (p - clusters[-1][-1]) / clusters[-1][-1] < cluster_pct:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    tested = [sum(c) / len(c) for c in clusters if len(c) >= 2]
    above = [p for p in tested if p > entry_price]
    return min(above) if above else None


# ── N1 / Stage 1: is the live R/R metric predictive at all? ────────────────────
# The live swing gate rejects entries whose R/R = (first resistance - entry) /
# (entry - chart stop) falls below the threshold — but NO exit path ever trades
# to that target (exits are stop / trailing stop / stall / thesis break). This
# replays historical setups, assigns each the R/R the live formula would have
# produced, simulates the REAL exit engine, and asks whether R/R sorted outcomes.
#
# PRE-REGISTERED VERDICT RULE (written before the first run, 2026-08-26):
#   PREDICTIVE  — entries below the loose gate (R/R < 1.2) underperform those at
#                 or above it by >3 percentage points of average realized return,
#                 AND the bucket means rise broadly with R/R. Then the gate earns
#                 its keep, an empty swing book is CORRECT in this regime, and the
#                 fix is to stop paying for vision on setups that cannot qualify.
#   NOT PREDICTIVE — the sub-1.2 bucket performs as well as or better than the
#                 >=1.2 bucket (within 3 pts, or better). Then the gate is
#                 discarding tradeable setups and the METRIC is the problem;
#                 proceed to Stage 2 (replace the reward leg / regate).
#   Either way this changes NO trading logic on its own.

def rr_predictiveness_backtest(raw=None, tickers=None, entries=None) -> dict:
    import numpy as np

    if raw is None or entries is None:
        _, entries, raw = screener_backtest()

    # Current live SWING exit engine (E12): S1 - 0.5*ATR primary, 2.5*ATR only
    # as the no-S1 fallback. NOTE the variant literally labelled "live" in
    # EXIT_VARIANTS is the PRE-E12 config (it carries the 2.5*ATR floor) — using
    # it here would simulate stops the swing book no longer uses.
    cfg = dict(use_s1=True, atr_mult=None, trail_at=0.30, trail_pct=0.15,
               stall=True, time_stop=None)

    if len(entries) > MAX_ENTRIES_SIM:
        random.seed(11)
        entries = random.sample(entries, MAX_ENTRIES_SIM)

    rows = []
    arrays = {}
    for t, i, n_sig, date in entries:
        try:
            if t not in arrays:
                df = raw[t].dropna(subset=["Close"])
                arrays[t] = (df["Close"].values, df["Low"].values, df["High"].values,
                             df["Volume"].values, df["Volume"].rolling(90).mean().values)
            closes, lows, highs, vols, v90s = arrays[t]
            if i + 2 >= len(closes) or i < 25:
                continue
            entry = float(closes[i])
            seg_hi = highs[i - 20:i + 1]
            seg_lo = lows[i - 20:i + 1]
            seg_cl = closes[i - 21:i + 1]
            tr = [max(seg_hi[j] - seg_lo[j], abs(seg_hi[j] - seg_cl[j]),
                      abs(seg_lo[j] - seg_cl[j])) for j in range(len(seg_hi))]
            atr = float(np.mean(tr))
            if atr <= 0:
                continue
            s1 = _s1_support(lows, i, entry)
            r1 = _r1_resistance(highs, i, entry)
            stop = (s1 - 0.5 * atr) if s1 is not None else (entry - 2.5 * atr)
            risk = entry - stop
            if risk <= 0 or r1 is None:
                continue
            rr = (r1 - entry) / risk
            ret, hold, why = _simulate(closes, lows, vols, v90s, i, cfg, s1, atr)
            rows.append({"rr": rr, "ret": ret, "hold": hold, "why": why,
                         "n_sig": n_sig, "era": "2022-23" if date < "2024-01-01" else "2024-26"})
        except Exception:
            continue

    def stats(rs):
        if not rs:
            return {"n": 0}
        v = [r["ret"] for r in rs]
        return {"n": len(v), "avg": round(float(np.mean(v)), 4),
                "median": round(float(np.median(v)), 4),
                "win_rate": round(float(np.mean([x > 0 for x in v])), 3),
                "avg_hold": round(float(np.mean([r["hold"] for r in rs])), 1)}

    buckets = [("<0.5", 0.0, 0.5), ("0.5-1.2", 0.5, 1.2), ("1.2-2.0", 1.2, 2.0),
               ("2.0-3.0", 2.0, 3.0), (">=3.0", 3.0, 1e9)]
    by_bucket = {name: stats([r for r in rows if lo <= r["rr"] < hi])
                 for name, lo, hi in buckets}
    below = [r for r in rows if r["rr"] < 1.2]
    above = [r for r in rows if r["rr"] >= 1.2]
    b_s, a_s = stats(below), stats(above)
    gap = (a_s.get("avg", 0) - b_s.get("avg", 0)) * 100 if below and above else None

    verdict = "INCONCLUSIVE (insufficient sample)"
    if gap is not None and min(len(below), len(above)) >= 50:
        verdict = ("PREDICTIVE — gate earns its keep" if gap > 3.0
                   else "NOT PREDICTIVE — metric is the problem, proceed to Stage 2")

    report = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_setups": len(rows),
        "exit_engine": "E12 live swing (S1-0.5ATR, no ATR floor, trail@30/15, stall)",
        "rr_source": "numeric mirror of vision target: nearest tested resistance above entry",
        "by_rr_bucket": by_bucket,
        "below_loose_gate_1.2": b_s,
        "at_or_above_loose_gate_1.2": a_s,
        "gap_pts_above_minus_below": round(gap, 2) if gap is not None else None,
        "by_era": {e: {"below_1.2": stats([r for r in below if r["era"] == e]),
                       "at_or_above_1.2": stats([r for r in above if r["era"] == e])}
                   for e in ("2022-23", "2024-26")},
        "verdict": verdict,
    }
    os.makedirs("data", exist_ok=True)
    with open("data/rr_predictiveness_backtest.json", "w") as f:
        json.dump(report, f, indent=1)
    return report


# ── E24 / Stage 1b: does ANY setup-quality x exit-policy combination show edge? ─
# Stage 1 (E23) found average realized return ~0% in every R/R bucket, but "0%" is
# meaningless without a benchmark: 0% over a 21-day hold is a win in 2022 and a
# loss in 2024. This measures every combination against a MATCHED-WINDOW SPY
# hold (same entry date, same holding period) so the comparison is like-for-like.
#
# PRE-REGISTERED VERDICT RULE (written before the first run, 2026-08-26):
#   EDGE EXISTS   — at least one (quality bucket x exit variant) cell beats its
#                   matched SPY benchmark by >2 pts of average return AND wins
#                   >50% of the time AND holds that sign in BOTH eras, at n>=50.
#                   Then the swing thesis is sound and the work is selection/exit
#                   tuning toward that cell.
#   NO EDGE       — no cell clears that bar. Then no entry-gate change can rescue
#                   the swing book and the honest recommendation is to stop running
#                   it in its current form, NOT to retune thresholds.
#   Cells failing only the era-consistency test are reported as FRAGILE, never as
#   edge — that is the overfitting trap this rule exists to block.
#   This function changes NO trading logic.

def swing_edge_backtest(raw=None, entries=None) -> dict:
    import numpy as np

    if raw is None or entries is None:
        _, entries, raw = screener_backtest()

    if len(entries) > MAX_ENTRIES_SIM:
        random.seed(11)
        entries = random.sample(entries, MAX_ENTRIES_SIM)

    spy_close = raw["SPY"]["Close"].dropna()
    spy_idx = {d.date().isoformat(): k for k, d in enumerate(spy_close.index)}
    spy_vals = spy_close.values

    rows = []
    arrays = {}
    for t, i, n_sig, date in entries:
        try:
            if t not in arrays:
                df = raw[t].dropna(subset=["Close"])
                arrays[t] = (df["Close"].values, df["Low"].values, df["High"].values,
                             df["Volume"].values, df["Volume"].rolling(90).mean().values,
                             df["Close"].rolling(50).mean().values,
                             df["Close"].rolling(200).mean().values)
            closes, lows, highs, vols, v90s, ma50, ma200 = arrays[t]
            if i + 2 >= len(closes) or i < 25:
                continue
            entry = float(closes[i])
            seg_hi, seg_lo = highs[i - 20:i + 1], lows[i - 20:i + 1]
            seg_cl = closes[i - 21:i + 1]
            tr = [max(seg_hi[j] - seg_lo[j], abs(seg_hi[j] - seg_cl[j]),
                      abs(seg_lo[j] - seg_cl[j])) for j in range(len(seg_hi))]
            atr = float(np.mean(tr))
            if atr <= 0:
                continue
            s1 = _s1_support(lows, i, entry)
            # Stage-2 proxy: price > MA50 > MA200 (cheap stand-in for the full template)
            m50, m200 = ma50[i], ma200[i]
            stage2 = bool(not np.isnan(m50) and not np.isnan(m200)
                          and entry > m50 > m200)
            sk = spy_idx.get(date)
            for name, cfg in EXIT_VARIANTS.items():
                ret, hold, why = _simulate(closes, lows, vols, v90s, i, cfg, s1, atr)
                # matched SPY: same entry date, same realized holding period
                bench = None
                if sk is not None and sk + hold < len(spy_vals):
                    bench = float(spy_vals[sk + hold] / spy_vals[sk] - 1)
                rows.append({"exit": name, "n_sig": min(n_sig, 4), "stage2": stage2,
                             "ret": ret, "bench": bench, "hold": hold,
                             "era": "2022-23" if date < "2024-01-01" else "2024-26"})
        except Exception:
            continue

    def cell(rs):
        rs = [r for r in rs if r["bench"] is not None]
        if not rs:
            return {"n": 0}
        v = [r["ret"] for r in rs]
        ex = [r["ret"] - r["bench"] for r in rs]
        return {"n": len(v),
                "avg": round(float(np.mean(v)), 4),
                "spy": round(float(np.mean([r["bench"] for r in rs])), 4),
                "excess": round(float(np.mean(ex)), 4),
                "win_rate": round(float(np.mean([x > 0 for x in v])), 3),
                "beat_spy": round(float(np.mean([x > 0 for x in ex])), 3)}

    grid = {}
    for name in EXIT_VARIANTS:
        for nsig in (2, 3, 4):
            for st in (True, False):
                sub = [r for r in rows if r["exit"] == name
                       and r["n_sig"] == nsig and r["stage2"] == st]
                key = f"{name} | {nsig}+sig | stage2={st}"
                c = cell(sub)
                if c.get("n", 0) >= 50:
                    c["by_era"] = {e: cell([r for r in sub if r["era"] == e])
                                   for e in ("2022-23", "2024-26")}
                    grid[key] = c

    winners, fragile = [], []
    for k, c in grid.items():
        if c["excess"] > 0.02 and c["win_rate"] > 0.50:
            eras = c.get("by_era", {})
            ok = all(eras.get(e, {}).get("excess", -1) > 0 for e in ("2022-23", "2024-26"))
            (winners if ok else fragile).append((k, c["excess"], c["win_rate"]))

    winners.sort(key=lambda x: -x[1])
    fragile.sort(key=lambda x: -x[1])
    report = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_setups": len({(r["n_sig"], r["era"], r["hold"], r["ret"]) for r in rows}),
        "n_cells": len(grid),
        "benchmark": "matched-window SPY (same entry date, same realized hold)",
        "verdict": ("EDGE EXISTS" if winners else
                    ("NO EDGE (some FRAGILE cells — era-inconsistent)" if fragile
                     else "NO EDGE")),
        "winners": [{"cell": k, "excess": e, "win_rate": w} for k, e, w in winners[:10]],
        "fragile_era_inconsistent": [{"cell": k, "excess": e, "win_rate": w}
                                     for k, e, w in fragile[:10]],
        "grid": grid,
    }
    os.makedirs("data", exist_ok=True)
    with open("data/swing_edge_backtest.json", "w") as f:
        json.dump(report, f, indent=1)
    return report


def run_all() -> dict:
    screener, entries, raw = screener_backtest()
    exits, exits_by_era = exit_backtest(entries, raw)
    result = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": {"first_signal": FIRST_SIGNAL, "fwd_cap_days": FWD_MAX},
        "screener": screener,
        "exit_engine": exits,
        "exit_engine_by_era": exits_by_era,
        "n_candidate_entries": len(entries),
    }
    os.makedirs("data", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(result, f, indent=1)
    logger.info(f"[StratBT] saved → {CACHE_FILE}")
    return result


# ── E14: Minervini Trend Template + RS-percentile backtest (2016-2026) ─────────
# Replays the full 8-point Trend Template weekly and compares its cohort against
# (a) all prefilter passers and (b) our current price_structure check.
# 10y window per user decision: 5 regimes (2016-17 bull, 2018 crash, 2019 bull,
# 2020 COVID, 2021 bull, 2022 bear, 2023 chop, 2024-26 bull) with tolerable
# survivorship; cohort comparisons partially cancel survivor inflation.
# Pre-registered: template cohort must beat BOTH baselines on fwd21 in a
# majority of years INCLUDING at least one down year (2018 or 2022).

def trend_template_backtest() -> dict:
    import numpy as np
    import pandas as pd
    import yfinance as yf
    from core.universe import load_universe

    uni = load_universe()
    tickers = sorted({t for t, _ in uni})
    logger.info(f"[E14] downloading {len(tickers)} tickers from 2015-01-01...")
    raw = yf.download(tickers + ["SPY"], start="2015-01-01", progress=False,
                      auto_adjust=True, group_by="ticker", threads=True)
    spy = raw["SPY"]["Close"].dropna()
    idx = spy.index
    week_last = spy.groupby([idx.isocalendar().year, idx.isocalendar().week]).tail(1).index
    snap_dates = [d for d in week_last
                  if d >= pd.Timestamp("2016-01-01") and d <= idx[-1] - pd.Timedelta(days=35)]

    # pass 1: per-ticker indicator frames
    frames = {}
    for t in tickers:
        try:
            df = raw[t].dropna(subset=["Close"])
            if len(df) < 320:
                continue
            c = df["Close"]
            f = pd.DataFrame(index=df.index)
            f["close"] = c
            f["ma50"] = c.rolling(50).mean()
            f["ma150"] = c.rolling(150).mean()
            f["ma200"] = c.rolling(200).mean()
            f["ma200_1m_ago"] = f["ma200"].shift(21)
            f["lo52"] = c.rolling(252).min()
            f["hi52"] = c.rolling(252).max()
            f["ret3m"] = c.pct_change(63)
            frames[t] = f
        except Exception:
            continue

    # pass 2: per snapshot, build cross-sectional RS ranks + cohorts
    cohorts = {"template_pass": [], "current_structure": [], "all_passers": []}
    by_year = {}
    for d in snap_dates:
        rows = {}
        for t, f in frames.items():
            if d not in f.index:
                continue
            i = f.index.get_loc(d)
            r = f.iloc[i]
            if any(pd.isna(r[k]) for k in ("ma50", "ma150", "ma200", "ma200_1m_ago",
                                           "lo52", "hi52", "ret3m")):
                continue
            if i + 63 >= len(f):
                continue
            fwd21 = float(f["close"].iloc[i + 21] / r["close"] - 1)
            fwd63 = float(f["close"].iloc[i + 63] / r["close"] - 1)
            rows[t] = (r, fwd21, fwd63)
        if len(rows) < 50:
            continue
        rets = sorted(v[0]["ret3m"] for v in rows.values())
        n = len(rets)
        year = str(d.year)
        for t, (r, fwd21, fwd63) in rows.items():
            price = r["close"]
            rs_pct = np.searchsorted(rets, r["ret3m"]) / n * 100
            template = (price > r["ma50"] > r["ma150"] > r["ma200"]
                        and r["ma200"] > r["ma200_1m_ago"]
                        and price >= 1.30 * r["lo52"]
                        and price >= 0.75 * r["hi52"]
                        and rs_pct >= 70)
            structure = (price / r["hi52"] - 1) >= -0.08 and \
                        price > r["ma50"] > r["ma200"] and r["ma200"] > r["ma200_1m_ago"]
            rec = (fwd21, year, fwd63)
            cohorts["all_passers"].append(rec)
            if template:
                cohorts["template_pass"].append(rec)
            if structure:
                cohorts["current_structure"].append(rec)
        by_year[year] = True

    def stats(rows):
        if not rows:
            return {"n": 0}
        v = [x[0] for x in rows]
        v63 = [x[2] for x in rows]
        return {"n": len(v), "avg": round(float(np.mean(v)), 4),
                "median": round(float(np.median(v)), 4),
                "win_rate": round(float(np.mean([x > 0 for x in v])), 3),
                "avg_63d": round(float(np.mean(v63)), 4),
                "win_rate_63d": round(float(np.mean([x > 0 for x in v63])), 3)}

    years = sorted(by_year)
    report = {"window": f"{snap_dates[0].date()} → {snap_dates[-1].date()}",
              "overall": {k: stats(v) for k, v in cohorts.items()},
              "per_year": {y: {k: stats([x for x in v if x[1] == y])
                               for k, v in cohorts.items()} for y in years}}
    os.makedirs("data", exist_ok=True)
    with open("data/trend_template_backtest.json", "w") as f:
        json.dump(report, f, indent=1)
    return report


# ── E20: RS-definition race — 3-month RS (current) vs weighted 12-month RS ─────
# Same snapshot/cohort methodology as E14; the ONLY difference between the two
# cohorts is the RS percentile fed into criterion 8 of the Trend Template.
# Weighted 12m RS = IBD/Minervini style: 40% most recent quarter, 20% each of
# the three older quarters (0.4·ret63 + 0.2·ret126 + 0.2·ret189 + 0.2·ret252).
# Pre-registered (see EXPERIMENTS.md E20): weighted cohort must beat the 3m
# cohort on BOTH fwd21 and fwd63 avg in a majority of years INCLUDING at least
# one down year (2018 or 2022) to replace ret3m in compute_trend_template.

def rs_variant_backtest() -> dict:
    import numpy as np
    import pandas as pd
    import yfinance as yf
    from core.universe import load_universe

    uni = load_universe()
    tickers = sorted({t for t, _ in uni})
    logger.info(f"[E20] downloading {len(tickers)} tickers from 2015-01-01...")
    raw = yf.download(tickers + ["SPY"], start="2015-01-01", progress=False,
                      auto_adjust=True, group_by="ticker", threads=True)
    spy = raw["SPY"]["Close"].dropna()
    idx = spy.index
    week_last = spy.groupby([idx.isocalendar().year, idx.isocalendar().week]).tail(1).index
    snap_dates = [d for d in week_last
                  if d >= pd.Timestamp("2016-01-01") and d <= idx[-1] - pd.Timedelta(days=35)]

    frames = {}
    for t in tickers:
        try:
            df = raw[t].dropna(subset=["Close"])
            if len(df) < 320:
                continue
            c = df["Close"]
            f = pd.DataFrame(index=df.index)
            f["close"] = c
            f["ma50"] = c.rolling(50).mean()
            f["ma150"] = c.rolling(150).mean()
            f["ma200"] = c.rolling(200).mean()
            f["ma200_1m_ago"] = f["ma200"].shift(21)
            f["lo52"] = c.rolling(252).min()
            f["hi52"] = c.rolling(252).max()
            f["ret3m"] = c.pct_change(63)
            f["rs_w12m"] = (0.4 * c.pct_change(63) + 0.2 * c.pct_change(126)
                            + 0.2 * c.pct_change(189) + 0.2 * c.pct_change(252))
            frames[t] = f
        except Exception:
            continue

    cohorts = {"template_rs3m": [], "template_rs_w12m": []}
    by_year = {}
    for d in snap_dates:
        rows = {}
        for t, f in frames.items():
            if d not in f.index:
                continue
            i = f.index.get_loc(d)
            r = f.iloc[i]
            if any(pd.isna(r[k]) for k in ("ma50", "ma150", "ma200", "ma200_1m_ago",
                                           "lo52", "hi52", "ret3m", "rs_w12m")):
                continue
            if i + 63 >= len(f):
                continue
            fwd21 = float(f["close"].iloc[i + 21] / r["close"] - 1)
            fwd63 = float(f["close"].iloc[i + 63] / r["close"] - 1)
            rows[t] = (r, fwd21, fwd63)
        if len(rows) < 50:
            continue
        rets3m = sorted(v[0]["ret3m"] for v in rows.values())
        retsw = sorted(v[0]["rs_w12m"] for v in rows.values())
        n = len(rows)
        year = str(d.year)
        for t, (r, fwd21, fwd63) in rows.items():
            price = r["close"]
            base = (price > r["ma50"] > r["ma150"] > r["ma200"]
                    and r["ma200"] > r["ma200_1m_ago"]
                    and price >= 1.30 * r["lo52"]
                    and price >= 0.75 * r["hi52"])
            if not base:
                continue
            rec = (fwd21, year, fwd63)
            if np.searchsorted(rets3m, r["ret3m"]) / n * 100 >= 70:
                cohorts["template_rs3m"].append(rec)
            if np.searchsorted(retsw, r["rs_w12m"]) / n * 100 >= 70:
                cohorts["template_rs_w12m"].append(rec)
        by_year[year] = True

    def stats(rows):
        if not rows:
            return {"n": 0}
        v = [x[0] for x in rows]
        v63 = [x[2] for x in rows]
        return {"n": len(v), "avg": round(float(np.mean(v)), 4),
                "median": round(float(np.median(v)), 4),
                "win_rate": round(float(np.mean([x > 0 for x in v])), 3),
                "avg_63d": round(float(np.mean(v63)), 4),
                "win_rate_63d": round(float(np.mean([x > 0 for x in v63])), 3)}

    years = sorted(by_year)
    per_year = {y: {k: stats([x for x in v if x[1] == y]) for k, v in cohorts.items()}
                for y in years}
    # Verdict per pre-registration: weighted must beat 3m on BOTH horizons' avg,
    # majority of years, including >=1 down year (2018 or 2022)
    wins = [y for y in years
            if per_year[y]["template_rs_w12m"].get("n", 0) > 0
            and per_year[y]["template_rs3m"].get("n", 0) > 0
            and per_year[y]["template_rs_w12m"]["avg"] > per_year[y]["template_rs3m"]["avg"]
            and per_year[y]["template_rs_w12m"]["avg_63d"] > per_year[y]["template_rs3m"]["avg_63d"]]
    report = {"window": f"{snap_dates[0].date()} → {snap_dates[-1].date()}",
              "overall": {k: stats(v) for k, v in cohorts.items()},
              "per_year": per_year,
              "w12m_wins_both_horizons": wins,
              "majority": len(wins) > len(years) / 2,
              "includes_down_year": any(y in wins for y in ("2018", "2022"))}
    os.makedirs("data", exist_ok=True)
    with open("data/rs_variant_backtest.json", "w") as f:
        json.dump(report, f, indent=1)
    return report
