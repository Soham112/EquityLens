"""
Backfill signal_outcomes.jsonl from historical daily_scan_*.json files.

The signal tracker only began recording on 2026-07-02, so June's BUY signals
were never captured — this replays the saved scan files into the outcomes log
with real entry prices (the signal-day close, batch-fetched) so the 90-day
feedback clock starts from the actual signal dates.

Also patches stop_price onto existing rows that predate the field.

Idempotent: (ticker, signal_date) pairs already recorded are skipped.

Run:
  PYTHONPATH=. .venv/bin/python workflows/backfill_signals.py
"""
import datetime
import glob
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.signal_tracker import SignalOutcome, _load_outcomes, _save_outcomes

logger = logging.getLogger(__name__)


def backfill() -> int:
    outcomes = _load_outcomes()
    existing = {(o.ticker, o.signal_date) for o in outcomes}
    by_key = {(o.ticker, o.signal_date): o for o in outcomes}

    scan_files = sorted(glob.glob("data/daily_scan_*.json"))
    candidates: list[dict] = []
    for path in scan_files:
        m = re.search(r"daily_scan_(\d{4}-\d{2}-\d{2})\.json", path)
        if not m:
            continue
        scan_date = m.group(1)
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"skip {path}: {e}")
            continue
        for r in data.get("results", []):
            if r.get("signal") != "BUY":
                continue
            key = (r["ticker"], scan_date)
            if key in existing:
                # Patch stop_price onto pre-existing rows that lack it
                o = by_key[key]
                if o.stop_price is None and r.get("stop_tier3"):
                    o.stop_price = r["stop_tier3"]
                continue
            lt = r.get("lt_chart") or {}
            candidates.append({
                "ticker": r["ticker"],
                "signal_date": scan_date,
                "conviction": r.get("conviction", 0),
                "hunter_score": r.get("hunter_score", 0),
                "sentiment_boost": r.get("sentiment_boost", 0),
                "sector": r.get("sector", "unknown"),
                "entry_type": lt.get("entry_type"),
                "stop_price": r.get("stop_tier3"),
            })

    if not candidates:
        _save_outcomes(outcomes)
        print("Nothing to backfill (stop_price patches saved if any).")
        return 0

    # One batch download covering all signal dates
    import pandas as pd
    import yfinance as yf
    tickers = sorted({c["ticker"] for c in candidates})
    earliest = min(c["signal_date"] for c in candidates)
    print(f"Backfilling {len(candidates)} signals across {len(tickers)} tickers "
          f"(from {earliest})...")
    # yfinance history is ALWAYS split-adjusted to present (auto_adjust only
    # controls dividends), but the log's convention is raw-at-signal-time —
    # matching live recording and the scan files' stops. So the adjusted close
    # is multiplied back up by any split factor since the signal date;
    # update_outcomes divides it back out at scoring time.
    from core.signal_tracker import _split_factors, _factor_since
    split_events = _split_factors(tickers)
    raw = yf.download(tickers, start=earliest, progress=False,
                      auto_adjust=False, group_by="ticker")
    multi = isinstance(raw.columns, pd.MultiIndex)
    available = set(raw.columns.get_level_values(0)) if multi else None

    added = 0
    for c in candidates:
        try:
            closes = (raw[c["ticker"]]["Close"] if multi else raw["Close"]).dropna()
            # Entry = close on the signal date (or the first bar after, if the
            # signal date was a non-trading day)
            signal_dt = datetime.date.fromisoformat(c["signal_date"])
            on_or_after = closes[closes.index.date >= signal_dt]
            if on_or_after.empty:
                logger.warning(f"{c['ticker']} {c['signal_date']}: no price bar — skipped")
                continue
            factor = _factor_since(split_events.get(c["ticker"], []), signal_dt)
            entry_price = round(float(on_or_after.iloc[0]) * factor, 4)
        except Exception as e:
            logger.warning(f"{c['ticker']} {c['signal_date']}: {e} — skipped")
            continue

        outcomes.append(SignalOutcome(
            ticker=c["ticker"],
            signal_date=c["signal_date"],
            signal="BUY",
            conviction=c["conviction"],
            entry_price=entry_price,
            price_30d=None, price_90d=None,
            return_30d=None, return_90d=None,
            hit=None,
            hunter_score=c["hunter_score"],
            sentiment_boost=c["sentiment_boost"],
            sector=c["sector"],
            entry_type=c["entry_type"],
            stop_price=c["stop_price"],
        ))
        added += 1

    outcomes.sort(key=lambda o: (o.signal_date, o.ticker))
    _save_outcomes(outcomes)
    print(f"Backfilled {added} signals ({len(outcomes)} total in log).")
    return added


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    backfill()
