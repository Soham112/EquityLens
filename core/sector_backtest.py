"""
Sector Funnel Backtest (E9 Phase 1) — replays the weekly sector-ranking formula
over ~5 years of history and measures what it would have picked, BEFORE we
trust it in production.

Replicates agents/scout.py's composite exactly (same normalizations, same
inputs — the live funnel also scores macro sectors off their ETF's price and
volume, so this is a high-fidelity replay, not an approximation):

    composite = w_ret * norm(vs_spy_60d)  +  w_accel * norm(ret20 - ret60)
              + w_breadth * norm(vol20/vol90)

Outputs (cached to data/sector_backtest.json, served by /api/backtest/sectors):
  - weekly full rankings + forward 1/4/12-week returns per sector
  - rotation equity curves: formula top-3 vs SPY vs equal-weight all 10
  - hit metrics: how often the funnel's top-3 caught the best sector,
    how often ranks 4-6 hid the winner
  - cutoff analysis (top-3 vs top-4 vs top-5) and alternative weight grid
  - per-regime breakdown (2021 bull / 2022 bear / 2023 chop / 2024-25 bull / 2026)
  - plain-language verdicts

One batch yf.download for 11 tickers — no per-ticker loops (see CLAUDE.md).
"""
import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

CACHE_FILE = "data/sector_backtest.json"
START = "2020-09-01"          # ~4 months of warmup before first 2021 signal
FIRST_SIGNAL = "2021-01-01"

SECTOR_ETFS = {
    "technology": "XLK", "communication_services": "XLC", "financials": "XLF",
    "healthcare": "XLV", "industrials": "XLI", "consumer_discretionary": "XLY",
    "energy": "XLE", "utilities": "XLU", "real_estate": "XLRE", "materials": "XLB",
}

REGIMES = [
    ("2021 bull",      "2021-01-01", "2021-12-31"),
    ("2022 bear",      "2022-01-01", "2022-12-31"),
    ("2023 chop",      "2023-01-01", "2023-12-31"),
    ("2024-25 bull",   "2024-01-01", "2025-12-31"),
    ("2026 YTD",       "2026-01-01", "2099-01-01"),
]

# Weight grids to test: (relative_return, momentum_accel, volume_breadth)
WEIGHT_GRID = [
    ("live 50/30/20", 0.50, 0.30, 0.20),
    ("return only",   1.00, 0.00, 0.00),
    ("70/30/0",       0.70, 0.30, 0.00),
    ("equal thirds",  0.34, 0.33, 0.33),
    ("accel only",    0.00, 1.00, 0.00),
    ("60/20/20",      0.60, 0.20, 0.20),
    ("40/40/20",      0.40, 0.40, 0.20),
]


def _composite(vs_spy_60d: float, accel: float, breadth: float,
               w_ret: float, w_accel: float, w_breadth: float) -> float:
    """Same normalizations as agents/scout.py._composite_score."""
    ret_score = max(0.0, min(100.0, (vs_spy_60d + 0.30) / 0.60 * 100))
    accel_score = max(0.0, min(100.0, (accel + 0.20) / 0.40 * 100))
    breadth_score = max(0.0, min(100.0, (breadth - 0.5) / 1.5 * 100))
    return ret_score * w_ret + accel_score * w_accel + breadth_score * w_breadth


def run_backtest() -> dict:
    import numpy as np
    import pandas as pd
    import yfinance as yf

    tickers = list(SECTOR_ETFS.values()) + ["SPY"]
    logger.info(f"[SectorBT] downloading {len(tickers)} ETFs from {START}...")
    raw = yf.download(tickers, start=START, progress=False,
                      auto_adjust=True, group_by="ticker")
    close = pd.DataFrame({t: raw[t]["Close"] for t in tickers}).dropna(how="all")
    volume = pd.DataFrame({t: raw[t]["Volume"] for t in tickers})
    sectors = list(SECTOR_ETFS.keys())

    # Weekly signal dates: last trading day of each ISO week, with enough
    # history for the 90d volume window and at least 5 forward days
    idx = close.index
    week_last = close.groupby([idx.isocalendar().year, idx.isocalendar().week]).tail(1).index
    first_ts = pd.Timestamp(FIRST_SIGNAL)

    def ret(t, i, days):
        if i - days < 0 or pd.isna(close[t].iloc[i - days]) or pd.isna(close[t].iloc[i]):
            return None
        return float(close[t].iloc[i] / close[t].iloc[i - days] - 1)

    def fwd(t, i, days):
        if i + days >= len(close) or pd.isna(close[t].iloc[i]) or pd.isna(close[t].iloc[i + days]):
            return None
        return float(close[t].iloc[i + days] / close[t].iloc[i] - 1)

    weekly = []           # one record per signal week
    pos = {d: i for i, d in enumerate(idx)}
    for d in week_last:
        i = pos[d]
        if d < first_ts or i < 90:
            continue
        spy60 = ret("SPY", i, 60)
        if spy60 is None:
            continue
        feats, scores = {}, {}
        ok = True
        for s in sectors:
            etf = SECTOR_ETFS[s]
            r20, r60 = ret(etf, i, 20), ret(etf, i, 60)
            if r20 is None or r60 is None:
                ok = False
                break
            v20 = float(volume[etf].iloc[max(0, i - 19): i + 1].mean())
            v90 = float(volume[etf].iloc[max(0, i - 89): i + 1].mean())
            feats[s] = (r60 - spy60, r20 - r60, (v20 / v90) if v90 else 1.0)
            scores[s] = round(_composite(*feats[s], 0.50, 0.30, 0.20), 1)
        if not ok:
            continue
        ranking = sorted(sectors, key=lambda s: -scores[s])
        rec = {
            "date": d.date().isoformat(),
            "ranking": ranking,
            "scores": scores,
            "features": {s: [round(x, 4) for x in feats[s]] for s in sectors},
            "fwd_1w": {s: fwd(SECTOR_ETFS[s], i, 5) for s in sectors},
            "fwd_4w": {s: fwd(SECTOR_ETFS[s], i, 21) for s in sectors},
            "fwd_12w": {s: fwd(SECTOR_ETFS[s], i, 63) for s in sectors},
            "spy_fwd_1w": fwd("SPY", i, 5),
        }
        weekly.append(rec)

    # ── Hit metrics (weeks where 4w forward data exists) ──
    scored = [w for w in weekly if all(v is not None for v in w["fwd_4w"].values())]

    def top3_stats(recs, rank_key="ranking"):
        hits, top3_rets, excl_rets, hidden_wins = 0, [], [], 0
        for w in recs:
            f = w["fwd_4w"]
            best = max(sectors, key=lambda s: f[s])
            top3 = w[rank_key][:3]
            if best in top3:
                hits += 1
            top3_rets.append(sum(f[s] for s in top3) / 3)
            excl_rets.append(sum(f[s] for s in w[rank_key][3:]) / 7)
            # a rank 4-6 sector beating EVERY chosen sector = true hidden winner
            if any(f[s] > max(f[t] for t in top3) for s in w[rank_key][3:6]):
                hidden_wins += 1
        n = len(recs)
        return {
            "weeks": n,
            "best_in_top3_pct": round(hits / n, 3) if n else None,
            "avg_top3_fwd4w": round(sum(top3_rets) / n, 4) if n else None,
            "avg_excluded_fwd4w": round(sum(excl_rets) / n, 4) if n else None,
            "rank456_beat_all_top3_pct": round(hidden_wins / n, 3) if n else None,
        }

    overall = top3_stats(scored)

    # Cutoff analysis: does widening to top-4/5 capture the best sector much more?
    cutoffs = {}
    for k in (3, 4, 5):
        hits = sum(1 for w in scored
                   if max(sectors, key=lambda s: w["fwd_4w"][s]) in w["ranking"][:k])
        cutoffs[f"top{k}"] = round(hits / len(scored), 3) if scored else None

    # ── Alternative weight grid ──
    weights = []
    for name, wr, wa, wb in WEIGHT_GRID:
        rets = []
        for w in scored:
            sc = {s: _composite(*w["features"][s], wr, wa, wb) for s in sectors}
            top3 = sorted(sectors, key=lambda s: -sc[s])[:3]
            rets.append(sum(w["fwd_4w"][s] for s in top3) / 3)
        weights.append({
            "name": name,
            "avg_top3_fwd4w": round(sum(rets) / len(rets), 4) if rets else None,
        })
    weights.sort(key=lambda x: -(x["avg_top3_fwd4w"] or -9))

    # ── Rotation equity curves (weekly compounding of fwd_1w) ──
    curves = {"formula_top3": [1.0], "spy": [1.0], "equal_weight": [1.0]}
    curve_dates = []
    for w in weekly:
        f1 = w["fwd_1w"]
        if w["spy_fwd_1w"] is None or any(f1[s] is None for s in sectors):
            continue
        curve_dates.append(w["date"])
        top3 = w["ranking"][:3]
        curves["formula_top3"].append(curves["formula_top3"][-1] * (1 + sum(f1[s] for s in top3) / 3))
        curves["spy"].append(curves["spy"][-1] * (1 + w["spy_fwd_1w"]))
        curves["equal_weight"].append(curves["equal_weight"][-1] * (1 + sum(f1[s] for s in sectors) / 10))
    for k in curves:
        curves[k] = [round(v, 4) for v in curves[k][1:]]

    # ── Per-regime breakdown ──
    regimes = []
    for name, lo, hi in REGIMES:
        recs = [w for w in scored if lo <= w["date"] <= hi]
        in_range = close.loc[(close.index >= lo) & (close.index <= hi)]
        if len(in_range) < 10:
            continue
        sector_total = {s: round(float(in_range[SECTOR_ETFS[s]].iloc[-1] /
                                       in_range[SECTOR_ETFS[s]].iloc[0] - 1), 4)
                        for s in sectors}
        ranked = sorted(sector_total, key=lambda s: -sector_total[s])
        top3_counts = {}
        for w in weekly:
            if lo <= w["date"] <= hi:
                for s in w["ranking"][:3]:
                    top3_counts[s] = top3_counts.get(s, 0) + 1
        regimes.append({
            "name": name,
            "start": in_range.index[0].date().isoformat(),
            "end": in_range.index[-1].date().isoformat(),
            "spy_return": round(float(in_range["SPY"].iloc[-1] / in_range["SPY"].iloc[0] - 1), 4),
            "sector_returns": sector_total,
            "best_sectors": ranked[:3],
            "worst_sectors": ranked[-3:][::-1],
            "funnel": top3_stats(recs) if recs else None,
            "most_picked": sorted(top3_counts, key=lambda s: -top3_counts[s])[:3],
        })

    # ── Sector summary over the full window ──
    full = close.loc[close.index >= FIRST_SIGNAL]
    sector_summary = []
    for s in sectors:
        weeks_top3 = sum(1 for w in weekly if s in w["ranking"][:3])
        weeks_top1 = sum(1 for w in weekly if w["ranking"][0] == s)
        sector_summary.append({
            "sector": s,
            "total_return": round(float(full[SECTOR_ETFS[s]].iloc[-1] /
                                        full[SECTOR_ETFS[s]].iloc[0] - 1), 4),
            "weeks_in_top3": weeks_top3,
            "weeks_rank1": weeks_top1,
        })
    sector_summary.sort(key=lambda x: -x["total_return"])
    spy_total = round(float(full["SPY"].iloc[-1] / full["SPY"].iloc[0] - 1), 4)

    # ── Plain-language verdicts ──
    verdicts = []
    edge = (overall["avg_top3_fwd4w"] or 0) - (overall["avg_excluded_fwd4w"] or 0)
    if edge > 0.002:
        verdicts.append(
            f"The formula's top-3 averaged {overall['avg_top3_fwd4w']:+.2%} over the next 4 weeks vs "
            f"{overall['avg_excluded_fwd4w']:+.2%} for excluded sectors ({overall['weeks']} weeks) — "
            f"the ranking has a real edge of {edge:+.2%}/4w.")
    elif edge < -0.002:
        verdicts.append(
            f"Excluded sectors BEAT the formula's top-3 ({overall['avg_excluded_fwd4w']:+.2%} vs "
            f"{overall['avg_top3_fwd4w']:+.2%} avg 4w over {overall['weeks']} weeks) — "
            f"the ranking formula is costing money and needs recalibration.")
    else:
        verdicts.append(
            f"No meaningful edge: top-3 {overall['avg_top3_fwd4w']:+.2%} vs excluded "
            f"{overall['avg_excluded_fwd4w']:+.2%} avg 4w — the funnel mainly buys focus, not alpha.")
    verdicts.append(
        f"The eventual best sector was inside the top-3 only {overall['best_in_top3_pct']:.0%} of weeks; "
        f"a rank-4-6 sector beat ALL three picks {overall['rank456_beat_all_top3_pct']:.0%} of weeks — "
        f"that is the funnel's blind-spot rate.")
    if cutoffs.get("top5") and cutoffs.get("top3"):
        verdicts.append(
            f"Widening the cutoff: top-3 catches the best sector {cutoffs['top3']:.0%} of the time, "
            f"top-4 {cutoffs['top4']:.0%}, top-5 {cutoffs['top5']:.0%}.")
    best_w = weights[0]
    live_w = next(x for x in weights if x["name"] == "live 50/30/20")
    if best_w["name"] != "live 50/30/20" and (best_w["avg_top3_fwd4w"] or 0) > (live_w["avg_top3_fwd4w"] or 0) + 0.001:
        verdicts.append(
            f"Weight test: '{best_w['name']}' ranked sectors better ({best_w['avg_top3_fwd4w']:+.2%} avg 4w "
            f"vs live's {live_w['avg_top3_fwd4w']:+.2%}) — candidate for recalibration (E10).")
    else:
        verdicts.append(
            f"Weight test: the live 50/30/20 mix held up — no tested alternative beat it meaningfully.")
    if curves["formula_top3"] and curves["spy"]:
        verdicts.append(
            f"Rotation simulation since 2021: formula top-3 rebalanced weekly → "
            f"{curves['formula_top3'][-1]:.2f}x vs SPY {curves['spy'][-1]:.2f}x vs "
            f"equal-weight-all-sectors {curves['equal_weight'][-1]:.2f}x.")

    result = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window": {"start": weekly[0]["date"] if weekly else None,
                   "end": weekly[-1]["date"] if weekly else None,
                   "weeks": len(weekly), "weeks_scored_4w": len(scored)},
        "spy_total_return": spy_total,
        "overall": overall,
        "cutoffs": cutoffs,
        "weights": weights,
        "regimes": regimes,
        "sector_summary": sector_summary,
        "curves": {"dates": curve_dates, **curves},
        "leadership": [{"date": w["date"], "top1": w["ranking"][0]} for w in weekly],
        "verdicts": verdicts,
    }
    os.makedirs("data", exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(result, f)
    logger.info(f"[SectorBT] done: {len(weekly)} weeks replayed, cached to {CACHE_FILE}")
    return result


def load_or_run(refresh: bool = False) -> dict:
    if not refresh and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return run_backtest()
