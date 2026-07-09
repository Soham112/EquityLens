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
                    entries.append((t, i, n))
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
    for t, i, n_sig in entries:
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
            results[name].append((r, hold))
            reasons[name][why] = reasons[name].get(why, 0) + 1

    report = {}
    for name, rs in results.items():
        if not rs:
            continue
        rets = [r for r, _ in rs]
        holds = [h for _, h in rs]
        report[name] = {
            "trades": len(rets),
            "avg_return": round(float(np.mean(rets)), 4),
            "median_return": round(float(np.median(rets)), 4),
            "win_rate": round(float(np.mean([r > 0 for r in rets])), 3),
            "avg_hold_days": round(float(np.mean(holds)), 1),
            "worst": round(float(min(rets)), 4),
            "exit_reasons": reasons[name],
        }
    return report


def run_all() -> dict:
    screener, entries, raw = screener_backtest()
    exits = exit_backtest(entries, raw)
    result = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": {"first_signal": FIRST_SIGNAL, "fwd_cap_days": FWD_MAX},
        "screener": screener,
        "exit_engine": exits,
        "n_candidate_entries": len(entries),
    }
    os.makedirs("data", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(result, f, indent=1)
    logger.info(f"[StratBT] saved → {CACHE_FILE}")
    return result
