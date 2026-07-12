"""
E17 backtest — does an EPS-growth-VELOCITY factor rank forward winners better
than EPS-growth LEVEL (and better than the universe average) in the growth
universe, era-split stable?

Design (pre-registered in EXPERIMENTS.md E17):
  Universe : GROWTH_UNIVERSE (61 curated small/mid-cap growth names).
  Point-in-time EPS: yfinance get_earnings_dates() gives Reported EPS tagged with
     the actual report date. As of a rebalance date D we use ONLY quarters whose
     report date < D — no lookahead.
  Factors per (ticker, D), needing >=5 reported quarters with a POSITIVE
     year-ago base (the factor only speaks to companies that actually earn —
     exactly the profitable-compounder case the book is about):
       YoY   = eps_yoy of most recent reported quarter  (growth LEVEL)
       VELO  = eps_yoy_latest - eps_yoy_prev            (growth ACCELERATION / velocity)
  Forward return: close(D+H)/close(D)-1 for H in {63, 126} trading days.
  Rankings each date (>=5 valid names required to form a quintile):
       EQL  = all valid candidates (universe average / no factor)
       YoY  = top quintile by YoY level
       VELO = top quintile by acceleration
  Aggregate mean fwd return, hit rate (>0) and beat-EQL rate, overall + per-year
     + two eras (2016-2020, 2021-2026).

Success (pre-registered): VELO top quintile beats EQL AND beats YoY on fwd63,
  in BOTH eras. If VELO ~= EQL -> factor has no edge -> E17 FAILS (stop, like E14).

Known limitation: the universe is today's survivorship-biased list, so ABSOLUTE
  returns are inflated. The test is RELATIVE (VELO vs YoY vs EQL inside the same
  universe), which that bias hits equally, so the factor comparison stays valid.
  yfinance-only, no paid API.
"""
import json
import os
import sys
import datetime as dt
from collections import defaultdict

import numpy as np
import pandas as pd
import yfinance as yf

from core.growth_universe import GROWTH_UNIVERSE

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PRICE_CACHE = os.path.join(CACHE_DIR, "e17_prices.parquet")
EPS_CACHE = os.path.join(CACHE_DIR, "e17_eps.json")

TICKERS = [t for t, _ in GROWTH_UNIVERSE]
HORIZONS = [63, 126]
MIN_QUARTERS = 5          # need q and q-4 for YoY, plus one more for velocity
MIN_NAMES_FOR_QUINTILE = 5
QUINTILE = 0.20

# Quarter-start rebalance dates 2016..2025 (leave >=6mo fwd runway before today)
REBAL = [f"{y}-{m:02d}-01" for y in range(2016, 2026) for m in (1, 4, 7, 10)]
REBAL = [d for d in REBAL if d <= "2025-12-01"]


def load_prices() -> pd.DataFrame:
    if os.path.exists(PRICE_CACHE):
        return pd.read_parquet(PRICE_CACHE)
    print(f"Downloading prices for {len(TICKERS)} tickers 2015-06..2026-07 ...")
    df = yf.download(TICKERS, start="2015-06-01", end="2026-07-01",
                     auto_adjust=True, progress=False)["Close"]
    df.to_parquet(PRICE_CACHE)
    return df


def load_eps() -> dict:
    if os.path.exists(EPS_CACHE):
        return json.load(open(EPS_CACHE))
    out = {}
    for i, t in enumerate(TICKERS):
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=60)
            if ed is None or ed.empty or "Reported EPS" not in ed.columns:
                out[t] = []
                continue
            ed = ed.dropna(subset=["Reported EPS"])
            # index = report datetime (tz aware); store (iso_date, eps)
            rows = [(idx.date().isoformat(), float(v))
                    for idx, v in ed["Reported EPS"].items()]
            rows.sort(key=lambda r: r[0])   # oldest first
            out[t] = rows
        except Exception as e:
            print(f"  EPS fetch {t} failed: {e}")
            out[t] = []
        if (i + 1) % 10 == 0:
            print(f"  ...EPS {i+1}/{len(TICKERS)}")
    json.dump(out, open(EPS_CACHE, "w"))
    return out


def eps_factors(eps_rows, as_of: str):
    """Return (yoy_level, velocity) using only quarters reported strictly before as_of,
    or None if insufficient / unclean (non-positive year-ago base)."""
    hist = [(d, v) for d, v in eps_rows if d < as_of]
    if len(hist) < MIN_QUARTERS:
        return None
    vals = [v for _, v in hist]           # oldest..newest
    # YoY series where base (q-4) is positive
    yoy = []
    for i in range(4, len(vals)):
        base = vals[i - 4]
        if base is None or base <= 0:
            yoy.append(None)
        else:
            yoy.append((vals[i] - base) / abs(base))
    clean = [(i, y) for i, y in enumerate(yoy) if y is not None]
    if len(clean) < 2:
        return None
    yoy_latest = clean[-1][1]
    yoy_prev = clean[-2][1]
    return yoy_latest, yoy_latest - yoy_prev


def fwd_return(prices: pd.Series, as_of: str, horizon: int):
    """close(D+horizon)/close(D)-1 using trading-day offset; None if unavailable."""
    s = prices.dropna()
    if s.empty:
        return None
    idx = s.index.searchsorted(pd.Timestamp(as_of))
    if idx >= len(s):
        return None
    # first trading day on/after as_of
    start_i = idx
    end_i = start_i + horizon
    if end_i >= len(s):
        return None
    p0, p1 = s.iloc[start_i], s.iloc[end_i]
    if p0 <= 0:
        return None
    return p1 / p0 - 1.0


def run():
    prices = load_prices()
    eps = load_eps()
    prices.index = pd.to_datetime(prices.index)

    # records: (date, ticker, year, yoy, velo, {H: fwd})
    recs = []
    for d in REBAL:
        for t in TICKERS:
            f = eps_factors(eps.get(t, []), d)
            if f is None:
                continue
            if t not in prices.columns:
                continue
            fwds = {H: fwd_return(prices[t], d, H) for H in HORIZONS}
            if all(v is None for v in fwds.values()):
                continue
            recs.append({"date": d, "ticker": t, "year": int(d[:4]),
                         "yoy": f[0], "velo": f[1], "fwd": fwds})

    print(f"\nTotal (date,ticker) records with clean EPS factors + fwd price: {len(recs)}")

    # Per-date rankings -> collect forward returns for each strategy
    def summarize(picked, H):
        rs = [r["fwd"][H] for r in picked if r["fwd"][H] is not None]
        if not rs:
            return None
        rs = np.array(rs)
        return {"n": len(rs), "mean": float(rs.mean()),
                "median": float(np.median(rs)), "hit": float((rs > 0).mean())}

    # Bucket per era/year
    def era_of(y):
        return "2016-2020" if y <= 2020 else "2021-2026"

    # For each date, form EQL / YoY-top / VELO-top pools, tag with era/year
    tagged = []  # (era, year, strat, H, fwd)
    by_date = defaultdict(list)
    for r in recs:
        by_date[r["date"]].append(r)

    dates_used = 0
    for d, group in sorted(by_date.items()):
        # need forward price for a given horizon to count; build per horizon
        for H in HORIZONS:
            valid = [r for r in group if r["fwd"][H] is not None]
            if len(valid) < MIN_NAMES_FOR_QUINTILE:
                continue
            k = max(1, int(round(len(valid) * QUINTILE)))
            yoy_top = sorted(valid, key=lambda r: r["yoy"], reverse=True)[:k]
            velo_top = sorted(valid, key=lambda r: r["velo"], reverse=True)[:k]
            eql_mean = np.mean([r["fwd"][H] for r in valid])
            for strat, pool in (("EQL", valid), ("YoY", yoy_top), ("VELO", velo_top)):
                for r in pool:
                    tagged.append((era_of(r["year"]), r["year"], strat, H,
                                   r["fwd"][H], r["fwd"][H] > eql_mean))
        dates_used += 1

    tdf = pd.DataFrame(tagged, columns=["era", "year", "strat", "H", "fwd", "beat_eql"])

    def block(df, label):
        print(f"\n===== {label} =====")
        for H in HORIZONS:
            print(f"\n  Horizon fwd{H} trading days:")
            print(f"    {'strat':6} {'n':>5} {'mean%':>8} {'median%':>8} {'hit%':>7} {'beatEQL%':>9}")
            for strat in ("EQL", "YoY", "VELO"):
                sub = df[(df.strat == strat) & (df.H == H)]
                if sub.empty:
                    continue
                print(f"    {strat:6} {len(sub):>5} {sub.fwd.mean()*100:>8.2f} "
                      f"{sub.fwd.median()*100:>8.2f} {(sub.fwd>0).mean()*100:>7.1f} "
                      f"{sub.beat_eql.mean()*100:>9.1f}")

    block(tdf, "OVERALL 2016-2026")
    for era in ("2016-2020", "2021-2026"):
        block(tdf[tdf.era == era], f"ERA {era}")

    # Per-year VELO-vs-EQL edge on fwd63 (the pre-registered primary horizon)
    print("\n===== PER-YEAR: VELO minus EQL, fwd63 mean% (era-split stability) =====")
    print(f"    {'year':>6} {'EQL%':>8} {'VELO%':>8} {'YoY%':>8} {'V-EQL':>8} {'V-YoY':>8} {'nVELO':>6}")
    velo_beats_eql_years = 0
    velo_beats_yoy_years = 0
    total_years = 0
    for y in sorted(tdf.year.unique()):
        sub = tdf[(tdf.year == y) & (tdf.H == 63)]
        e = sub[sub.strat == "EQL"].fwd.mean() * 100 if len(sub[sub.strat=="EQL"]) else np.nan
        v = sub[sub.strat == "VELO"].fwd.mean() * 100 if len(sub[sub.strat=="VELO"]) else np.nan
        yo = sub[sub.strat == "YoY"].fwd.mean() * 100 if len(sub[sub.strat=="YoY"]) else np.nan
        nv = len(sub[sub.strat == "VELO"])
        if np.isnan(v) or np.isnan(e):
            continue
        total_years += 1
        if v > e:
            velo_beats_eql_years += 1
        if not np.isnan(yo) and v > yo:
            velo_beats_yoy_years += 1
        print(f"    {y:>6} {e:>8.2f} {v:>8.2f} {yo:>8.2f} {v-e:>8.2f} "
              f"{(v-yo) if not np.isnan(yo) else float('nan'):>8.2f} {nv:>6}")
    print(f"\n  VELO beat EQL in {velo_beats_eql_years}/{total_years} years")
    print(f"  VELO beat YoY in {velo_beats_yoy_years}/{total_years} years")
    print(f"  Rebalance dates used: {dates_used}")


if __name__ == "__main__":
    run()
