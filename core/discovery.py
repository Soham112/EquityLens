"""
Super-Performer Discovery (E15 Phase 1) — the small/mid-cap layer.

Minervini's point (Trade Like a Stock Market Wizard): superperformance mostly
happens in small/mid caps BEFORE they join the big indexes. Our scan universe
is S&P 500 + Nasdaq 100 — companies that already made it. This module scrapes
the S&P MidCap 400 + SmallCap 600 (~1,000 names in the sweet spot), runs the
E14-validated Trend Template + liquidity screen, and surfaces a weekly
shortlist for dossier research (Phase 2) — names only, never auto-entries.

Flow: discovery shortlist → (Phase 2) dossier research → user/agent decision →
growth_universe → Growth Hunter → the same gates as everything else.
No parallel pipeline; this only feeds the front door.

Runs Sunday via weekly_scan (one batch download). Output: data/discovery_{date}.json
Dashboard: "Super Performers" tab via /api/discovery.
"""
import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

DISCOVERY_FILE = "data/discovery_{date}.json"
MIN_DOLLAR_VOL = 5e6          # $5M/day — tradeable but not institutional-only
MIN_PRICE = 5.0               # no true penny stocks
SHORTLIST_N = 15


def _scrape_wiki_constituents(url: str) -> dict:
    """ticker → company name from a Wikipedia constituents table."""
    import requests
    import pandas as pd
    from io import StringIO
    import warnings
    warnings.filterwarnings("ignore", message="Unverified HTTPS")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, verify=False, timeout=20)
    for df in pd.read_html(StringIO(r.text)):
        if "Symbol" in df.columns:
            name_col = next((c for c in ("Security", "Company", "Company Name") if c in df.columns), None)
            return {str(t).replace(".", "-"): (str(df[name_col].iloc[i]) if name_col else "")
                    for i, t in enumerate(df["Symbol"].tolist())}
    return {}


def fetch_midsmall_universe() -> tuple[list[str], dict]:
    """S&P MidCap 400 + SmallCap 600 constituents (minus our main universe)
    plus a ticker → company-name map."""
    from core.universe import load_universe
    names = {}
    names.update(_scrape_wiki_constituents("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"))
    names.update(_scrape_wiki_constituents("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"))
    main = {t for t, _ in load_universe()}
    out = sorted({t for t in names if t and t not in main})
    logger.info(f"[Discovery] {len(names)} scraped → {len(out)} after main-universe dedup")
    return out, names


def discovery_scan() -> dict:
    """Weekly: Trend Template + liquidity over the mid/small universe →
    top-N shortlist by RS percentile. Pure math, one batch download."""
    import pandas as pd
    import yfinance as yf
    from core.screener import compute_trend_template

    tickers, names = fetch_midsmall_universe()
    if not tickers:
        return {"error": "scrape failed", "shortlist": []}

    logger.info(f"[Discovery] downloading {len(tickers)} mid/small caps (400d)...")
    raw = yf.download(tickers, period="400d", interval="1d", progress=False,
                      auto_adjust=True, group_by="ticker", threads=True)
    flags = compute_trend_template(raw, tickers)

    top_level = set(raw.columns.get_level_values(0)) if isinstance(raw.columns, pd.MultiIndex) else None
    rows = []
    for t, f in flags.items():
        if not f["template_pass"]:
            continue
        try:
            df = raw[t] if top_level else raw
            price = float(df["Close"].dropna().iloc[-1])
            dollar_vol = float((df["Close"] * df["Volume"]).tail(20).mean())
            if price < MIN_PRICE or dollar_vol < MIN_DOLLAR_VOL:
                continue
            rows.append({
                "ticker": t, "name": names.get(t, ""), "price": round(price, 2),
                "rs_pct": f["rs_pct"],
                "pct_off_52w_high": f["pct_off_52w_high"],
                "pct_above_52w_low": f["pct_above_52w_low"],
                "dollar_vol_20d": round(dollar_vol / 1e6, 1),   # $M
                "dossier": os.path.exists(f"data/dossiers/{t}.md"),
            })
        except Exception:
            continue

    rows.sort(key=lambda r: -r["rs_pct"])
    result = {
        "date": datetime.date.today().isoformat(),
        "universe_size": len(tickers),
        "template_passers": sum(1 for f in flags.values() if f["template_pass"]),
        "shortlist": rows[:SHORTLIST_N],
        "note": "Names only — research candidates for dossiers, never auto-entries. "
                "Approved names join growth_universe and face the same gates.",
    }
    os.makedirs("data", exist_ok=True)
    with open(DISCOVERY_FILE.format(date=result["date"]), "w") as f:
        json.dump(result, f, indent=1)
    logger.info(f"[Discovery] {result['template_passers']} pass template → "
                f"shortlist {len(result['shortlist'])}")
    return result


def load_latest_discovery() -> dict:
    import glob
    # discovery_2*.json: date-stamped scans only — discovery_admitted.json
    # (the E15 source-tag sidecar) matches the broader pattern and, sorting
    # after '2', would shadow the real shortlist (bit 2026-07-10)
    files = sorted(glob.glob("data/discovery_2*.json"), reverse=True)
    if not files:
        return {"shortlist": [], "note": "No discovery scan yet — runs Sundays."}
    with open(files[0]) as f:
        return json.load(f)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(discovery_scan(), indent=1))
