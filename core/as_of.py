"""
As-of price shim — run the EXISTING pipeline as if it were an earlier time today.

Why this exists: the scheduled daily scan fires at 9:35 AM ET. When a scan is
run manually later in the session (missed schedule, catch-up run), every price
path silently picks up the live intraday quote, so signals, fills and stop
checks reflect the afternoon rather than the morning the scan is supposed to
represent. This module makes a whole run coherent at one chosen instant.

How: every price path in the codebase funnels through `yf.download` or
`yf.Ticker(...).history(...)`. `install()` patches BOTH on the yfinance module
object itself, so all importers (data_layer, screener, paper_trading,
growth_paper_trading, momentum_monitor, swing_chart_analysis, macro_pulse, ...)
see truncated data without any of them changing.

For a daily/weekly frame whose LAST bar is today (or the current week), that bar
is rebuilt from 1-minute bars up to the cutoff:
    Open   = first 1m open at/after the session open
    High   = max high through the cutoff
    Low    = min low through the cutoff
    Close  = last 1m close at/before the cutoff
    Volume = summed 1m volume through the cutoff
Bars before today are untouched — history is history.

Coverage is NOT silently assumed. Tickers with no 1m data (delisted, illiquid,
some indices) keep their live bar and are recorded in `report()["uncovered"]`,
so a run can state exactly what it could not pin to the cutoff.

Usage:
    from core import as_of
    as_of.install("09:35")      # ET
    ...run the pipeline...
    print(as_of.report())
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

ET = "America/New_York"

_installed = False
_cutoff_hhmm: Optional[str] = None
_orig_download = None
_orig_ticker = None

# ticker -> truncated bar dict (or None when 1m data is unavailable)
_bar_cache: dict[str, Optional[dict]] = {}
_uncovered: set[str] = set()
_covered: set[str] = set()


def _today_et() -> datetime.date:
    return pd.Timestamp.now(tz=ET).date()


def _cutoff_ts() -> pd.Timestamp:
    h, m = (int(x) for x in _cutoff_hhmm.split(":"))
    return pd.Timestamp.now(tz=ET).normalize() + pd.Timedelta(hours=h, minutes=m)


def _disk_cache_path() -> str:
    import os
    os.makedirs("data", exist_ok=True)
    tag = (_cutoff_hhmm or "").replace(":", "")
    return os.path.join("data", f"as_of_1m_{_today_et()}_{tag}.json")


def _load_disk_cache() -> None:
    """Reuse bars fetched by an earlier run today — reruns cost zero requests."""
    import json
    import os
    p = _disk_cache_path()
    if not os.path.exists(p):
        return
    try:
        with open(p) as f:
            for t, bar in json.load(f).items():
                _bar_cache.setdefault(t, bar)
                (_covered if bar else _uncovered).add(t)
        logger.info(f"[as_of] reused {len(_bar_cache)} cached 1m bars from {p}")
    except Exception as e:
        logger.debug(f"[as_of] disk cache read: {e}")


def _save_disk_cache() -> None:
    import json
    try:
        with open(_disk_cache_path(), "w") as f:
            json.dump(_bar_cache, f)
    except Exception as e:
        logger.debug(f"[as_of] disk cache write: {e}")


def prefetch(tickers: list[str]) -> None:
    """Warm the 1m cache for a known ticker set in batches.

    Call this BEFORE a run that fetches per-ticker (the deep scan does), or the
    lazy path issues one extra 1m request per ticker — the per-ticker-loop
    antipattern CLAUDE.md warns about, which trips Yahoo's 401 'Invalid Crumb'
    throttle and makes the pipeline silently SKIP tickers.
    """
    tk = [t for t in dict.fromkeys(_norm(tickers)) if t and t not in _bar_cache]
    if not tk:
        return
    logger.info(f"[as_of] prefetching 1m bars for {len(tk)} tickers...")
    _fetch_1m(tk)
    _save_disk_cache()
    logger.info(f"[as_of] prefetch done — {len(_covered)} covered, "
                f"{len(_uncovered)} without intraday data")


def _fetch_1m(tickers: list[str]) -> None:
    """Batch-fetch today's 1m bars and cache the truncated aggregate per ticker."""
    import time
    todo = [t for t in tickers if t not in _bar_cache]
    if not todo:
        return
    cutoff = _cutoff_ts()
    for i in range(0, len(todo), 100):          # keep each request sane
        chunk = todo[i:i + 100]
        if i:
            time.sleep(0.4)                     # be gentle between batches
        try:
            raw = _orig_download(chunk, period="1d", interval="1m", progress=False,
                                 auto_adjust=True, group_by="ticker", threads=True)
        except Exception as e:
            logger.debug(f"[as_of] 1m batch failed: {e}")
            for t in chunk:
                _bar_cache[t] = None
            continue
        multi = isinstance(raw.columns, pd.MultiIndex)
        for t in chunk:
            try:
                df = raw[t] if multi else raw
                df = df.dropna(subset=["Close"])
                if df.empty:
                    _bar_cache[t] = None
                    continue
                idx = df.index
                if idx.tz is None:
                    idx = idx.tz_localize("UTC")
                df = df.set_axis(idx.tz_convert(ET))
                upto = df[df.index <= cutoff]
                if upto.empty:
                    _bar_cache[t] = None
                    continue
                _bar_cache[t] = {
                    "Open": float(upto["Open"].iloc[0]),
                    "High": float(upto["High"].max()),
                    "Low": float(upto["Low"].min()),
                    "Close": float(upto["Close"].iloc[-1]),
                    "Volume": float(upto["Volume"].sum()),
                }
            except Exception:
                _bar_cache[t] = None
    for t in todo:
        (_covered if _bar_cache.get(t) else _uncovered).add(t)


def _truncate(df: pd.DataFrame, ticker: str, interval: str) -> pd.DataFrame:
    """Rewrite the final bar of `df` to the cutoff, if that bar covers today."""
    if df is None or len(df) == 0 or "Close" not in df.columns:
        return df
    iv = (interval or "1d").lower()
    if iv.endswith(("m", "h")):                  # intraday request: clip instead
        try:
            idx = df.index
            if getattr(idx, "tz", None) is None:
                return df
            return df[idx.tz_convert(ET) <= _cutoff_ts()]
        except Exception:
            return df

    bar = _bar_cache.get(ticker)
    if not bar:
        return df
    try:
        last = df.index[-1]
        last_date = last.date() if hasattr(last, "date") else None
        if last_date is None:
            return df
        today = _today_et()
        # daily: last bar must BE today. weekly/monthly: today falls inside it.
        if iv.startswith("1d"):
            if last_date != today:
                return df
        else:
            if not (last_date <= today <= last_date + datetime.timedelta(days=6)):
                return df
        df = df.copy()
        for col, key in (("Open", "Open"), ("High", "High"),
                         ("Low", "Low"), ("Close", "Close"), ("Volume", "Volume")):
            if col not in df.columns:
                continue
            if iv.startswith("1d"):
                df.loc[df.index[-1], col] = bar[key]
            else:
                # weekly bar: blend the in-progress week with the truncated day
                if col == "Close":
                    df.loc[df.index[-1], col] = bar["Close"]
                elif col == "High":
                    df.loc[df.index[-1], col] = max(float(df[col].iloc[-1]), bar["High"])
                elif col == "Low":
                    df.loc[df.index[-1], col] = min(float(df[col].iloc[-1]), bar["Low"])
        return df
    except Exception as e:
        logger.debug(f"[as_of] truncate {ticker}: {e}")
        return df


def _norm(tickers) -> list[str]:
    if isinstance(tickers, str):
        return [t for t in tickers.replace(",", " ").split() if t]
    try:
        return [str(t) for t in tickers]
    except Exception:
        return []


def install(cutoff_hhmm: str = "09:35") -> None:
    """Patch yfinance so the whole process sees prices as of `cutoff_hhmm` ET today."""
    global _installed, _cutoff_hhmm, _orig_download, _orig_ticker
    if _installed:
        return
    _cutoff_hhmm = cutoff_hhmm
    _orig_download = yf.download
    _orig_ticker = yf.Ticker

    def patched_download(tickers, *args, **kwargs):
        raw = _orig_download(tickers, *args, **kwargs)
        interval = kwargs.get("interval", "1d")
        if str(interval).lower().endswith(("m", "h")):
            return raw                                    # caller wants intraday
        tk = _norm(tickers)
        _fetch_1m(tk)
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                lvl0 = set(raw.columns.get_level_values(0))
                out = {}
                for t in tk:
                    if t in lvl0:
                        out[t] = _truncate(raw[t], t, interval)
                if out:
                    raw = pd.concat(out, axis=1)
            elif len(tk) == 1:
                raw = _truncate(raw, tk[0], interval)
        except Exception as e:
            logger.debug(f"[as_of] download patch: {e}")
        return raw

    class PatchedTicker(_orig_ticker):
        def history(self, *args, **kwargs):
            hist = super().history(*args, **kwargs)
            interval = kwargs.get("interval", "1d")
            t = getattr(self, "ticker", None)
            if not t:
                return hist
            if not str(interval).lower().endswith(("m", "h")):
                _fetch_1m([t])
            return _truncate(hist, t, interval)

    yf.download = patched_download
    yf.Ticker = PatchedTicker
    _installed = True
    _load_disk_cache()
    logger.info(f"[as_of] installed — all prices truncated to {cutoff_hhmm} ET "
                f"({_today_et()})")


def uninstall() -> None:
    global _installed
    if not _installed:
        return
    yf.download = _orig_download
    yf.Ticker = _orig_ticker
    _installed = False


def report() -> dict:
    return {
        "cutoff_et": _cutoff_hhmm,
        "date": str(_today_et()),
        "covered": sorted(_covered),
        "uncovered": sorted(_uncovered),
        "n_covered": len(_covered),
        "n_uncovered": len(_uncovered),
    }
