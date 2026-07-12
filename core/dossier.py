"""
Super-Performer Dossiers (E15 Phase 2) — the research layer between the discovery
screen (Phase 1, names only) and a growth_universe graduation decision.

Two layers, by design:
  1. DATA SKELETON (this module, $0, deterministic): assembles every free number
     we already have — discovery metrics, fundamentals, technicals, and cached
     sentiment when present — into data/dossiers/{ticker}.md. Auto-runs Sunday
     right after discovery_scan; the "Dossier" column flips ✓ on its own.
  2. RESEARCH NOTES (the Sunday agent, $0): the qualitative "why / catalyst / how"
     is left as a clearly-marked placeholder the weekly-review task fills via web
     research. A future Phase 3 (Haiku) could synthesise it — budget-gated.

Clobber-safe: an existing dossier is never overwritten (preserves hand-added
notes) unless force=True. No paid API, no script-side web calls — touches no
gate/threshold/score, so it is not an experiment.
"""
import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

DOSSIER_DIR = "data/dossiers"
_CACHE_DIR = "data/bigdata_cache"

# One shared template with STEP 2.5 of the weekly-review task. The skeleton fills
# the DATA sections and leaves the qualitative research + Verdict marked PENDING;
# the Sunday agent fills those in-place. This sentinel is the compose gate — a
# dossier "needs research" iff it still contains PENDING_MARKER in its Verdict.
PENDING_MARKER = "PENDING RESEARCH"
_PENDING_VERDICT = f"_{PENDING_MARKER} — not yet reviewed (ADMIT / WATCH / PASS)._"


def needs_research(ticker: str) -> bool:
    """True if a dossier exists but its Verdict is still the PENDING sentinel —
    i.e. the skeleton is written but the Sunday agent hasn't researched it yet."""
    p = os.path.join(DOSSIER_DIR, f"{ticker}.md")
    if not os.path.exists(p):
        return False
    try:
        return PENDING_MARKER in open(p).read()
    except Exception:
        return False


def _pct(x, dp=1):
    try:
        return f"{float(x) * 100:+.{dp}f}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(x, dp=2, pre="", suf=""):
    try:
        return f"{pre}{float(x):,.{dp}f}{suf}"
    except (TypeError, ValueError):
        return "n/a"


def _load_sentiment(ticker: str) -> dict | None:
    p = os.path.join(_CACHE_DIR, f"{ticker}.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def _risk_flags(item: dict, fund, price, days_to_earnings) -> list[str]:
    flags = []
    above_low = item.get("pct_above_52w_low")
    if above_low is not None and above_low > 1.0:
        flags.append(f"Extended — {_pct(above_low, 0)} above its 52-week low (late to the move)")
    if (item.get("dollar_vol_20d") or 0) < 10:
        flags.append(f"Thin liquidity — ${item.get('dollar_vol_20d')}M/day (size carefully / slippage)")
    if days_to_earnings is not None and 0 <= days_to_earnings <= 10:
        flags.append(f"Earnings in {days_to_earnings}d — event risk / entry blackout")
    if fund is not None:
        if fund.debt_to_equity is not None and fund.debt_to_equity > 2:
            flags.append(f"High leverage — debt/equity {fund.debt_to_equity:.1f}")
        if fund.fcf is not None and fund.fcf < 0:
            flags.append("Negative free cash flow — burning cash")
        if fund.revenue_growth_trend == "DECELERATING":
            flags.append("Revenue growth DECELERATING — thesis at risk")
    return flags


def build_dossier(item: dict, force: bool = False) -> str | None:
    """Build + save one dossier from a discovery shortlist item.
    Returns the file path, or None if skipped (already exists and not force)."""
    ticker = item.get("ticker")
    if not ticker:
        return None
    os.makedirs(DOSSIER_DIR, exist_ok=True)
    path = os.path.join(DOSSIER_DIR, f"{ticker}.md")
    if os.path.exists(path) and not force:
        return None

    # ── free data pulls (each guarded; a dossier is still useful if some fail) ──
    fund = price = None
    try:
        from core.data_layer import fetch_fundamentals
        fund = fetch_fundamentals(ticker)
    except Exception as e:
        logger.debug(f"dossier {ticker}: fundamentals failed: {e}")
    try:
        from core.data_layer import fetch_price_data
        price = fetch_price_data(ticker)
    except Exception as e:
        logger.debug(f"dossier {ticker}: price failed: {e}")
    days_to_earnings = None
    next_e = None
    try:
        from core.earnings_calendar import get_next_earnings
        next_e = get_next_earnings(ticker)
        if next_e:
            days_to_earnings = (next_e - datetime.date.today()).days
    except Exception:
        pass
    sent = _load_sentiment(ticker)

    today = datetime.date.today().isoformat()
    name = item.get("name") or (getattr(price, "company_name", None) or "")
    L: list[str] = []
    L.append(f"# {name} ({ticker}) — Discovery Dossier")
    L.append(f"_Generated {today} · Super-Performer shortlist · RS pct {_num(item.get('rs_pct'), 0)} · **research candidate, NOT a buy recommendation**_")
    L.append("")

    # Snapshot
    mcap = getattr(price, "market_cap", None)
    L.append("## Snapshot")
    L.append(f"- **Price:** {_num(item.get('price'), 2, '$')}"
             + (f"  ·  **Market cap:** {_num(mcap/1e9, 2, '$', 'B')}" if mcap else "")
             + f"  ·  **$ Vol (20d):** {_num(item.get('dollar_vol_20d'), 1, '$', 'M')}")
    if sent and sent.get("company"):
        c = sent["company"]
        L.append(f"- **Sector / industry:** {c.get('sector', 'n/a')} / {c.get('industry', 'n/a')}")
    L.append("")

    # Why it made the screen
    L.append("## Why it made the screen (Minervini Trend Template)")
    L.append(f"- **RS percentile:** {_num(item.get('rs_pct'), 0)} / 100 (relative-strength rank vs the mid/small universe)")
    L.append(f"- **{_pct(item.get('pct_above_52w_low'), 0)}** above its 52-week low; **{_pct(item.get('pct_off_52w_high'), 1)}** from its 52-week high")
    if price is not None and getattr(price, "stage", None):
        L.append(f"- **Weinstein stage:** {price.stage}")
    L.append("")

    # Numbers snapshot (heading shared with STEP 2.5)
    L.append("## Numbers snapshot")
    if fund is not None:
        L.append(f"- **Revenue growth (YoY):** {_pct(fund.revenue_growth_yoy)}  ·  **Trend:** {fund.revenue_growth_trend or 'n/a'}")
        if fund.quarterly_rev_growth:
            qs = ", ".join(_pct(q, 0) for q in fund.quarterly_rev_growth[-4:])
            L.append(f"- **Last quarters (QoQ rev growth):** {qs}")
        L.append(f"- **Gross margin:** {_pct(fund.gross_margin, 0)}  ·  **Free cash flow:** {_num(fund.fcf/1e6, 0, '$', 'M') if fund.fcf is not None else 'n/a'}")
        L.append(f"- **Debt/equity:** {_num(fund.debt_to_equity, 2)}  ·  **P/E:** {_num(fund.pe_ratio, 1)}  ·  **PEG:** {_num(fund.peg_ratio, 2)}")
        L.append(f"- **Earnings beat rate:** {_pct(fund.earnings_beat_rate, 0)}  ·  **Avg surprise:** {_pct(fund.earnings_surprise_avg, 1)}")
    else:
        L.append("- _Fundamentals unavailable from the free source._")
    L.append("")

    # Momentum & timing
    L.append("## Momentum & timing")
    if price is not None:
        L.append(f"- **RS vs SPY (3mo):** {_pct(getattr(price, 'rs_vs_spy', None))}")
        if getattr(price, "atr_compression", None) is not None:
            coil = " (coiling)" if price.atr_compression < 0.7 else ""
            L.append(f"- **ATR compression:** {price.atr_compression:.2f}× 3-mo avg{coil}")
        if getattr(price, "macd_cross_bullish", None):
            L.append("- **MACD:** bullish cross in the last 3 days")
    if next_e and days_to_earnings is not None and days_to_earnings >= 0:
        L.append(f"- **Next earnings:** {next_e.isoformat()} (in {days_to_earnings}d — blackout if ≤ ~7)")
    elif next_e:
        L.append(f"- **Last earnings:** {next_e.isoformat()} (no confirmed upcoming date)")
    else:
        L.append("- **Next earnings:** n/a")
    L.append("")

    # Sentiment & insiders
    L.append("## Sentiment & insiders")
    if sent:
        try:
            sig = sent["sentiment"]["signals"]["sentiment"]
            L.append(f"- **Sentiment:** current {sig.get('current')} · momentum {sig.get('momentum')}")
        except Exception:
            pass
        ins = sent.get("yfinance_insider") or {}
        if ins:
            L.append(f"- **Insiders:** net {ins.get('net_signal', 'n/a')} · CEO/CFO buying: {ins.get('ceo_cfo_buying')}")
    else:
        L.append("- _No sentiment cache — this name is outside the weekly refresh set (mid/small cap)._")
    L.append("")

    # Risk flags
    flags = _risk_flags(item, fund, price, days_to_earnings)
    L.append("## Risk flags (auto-detected)")
    L.extend([f"- ⚠ {f}" for f in flags] if flags else ["- None auto-detected (still do your own work)."])
    L.append("")

    # ── Research sections: skeleton leaves these PENDING; the Sunday agent
    #    (weekly-review STEP 2.5) fills them in-place via web research. Headings
    #    are shared with that step so it edits, never re-templates. ──
    L.append("<!-- Research below — filled Sunday via web research (weekly-review STEP 2.5). -->")
    L.append("")
    for h in ("What they do", "The story / catalyst", "Management",
              "Sector mapping (informative, not a filter)", "Red flags (news / filings)"):
        L.append(f"## {h}")
        L.append("_pending research_")
        L.append("")
    L.append("## Verdict")
    L.append(_PENDING_VERDICT)
    L.append("")
    L.append("---")
    L.append("_Data sections auto-generated (core/dossier.py). Research sections + Verdict "
             "are filled Sunday via web research; ADMIT graduates the name to growth_universe._")

    with open(path, "w") as f:
        f.write("\n".join(L))
    logger.info(f"[Dossier] wrote {path} ({len(flags)} risk flag(s))")
    return path


def generate_dossiers(shortlist: list[dict] | None = None, force: bool = False) -> dict:
    """Build dossiers for a discovery shortlist (defaults to the latest scan).
    Skips names that already have one unless force=True. Returns a summary."""
    if shortlist is None:
        try:
            from core.discovery import load_latest_discovery
            shortlist = load_latest_discovery().get("shortlist", [])
        except Exception as e:
            logger.warning(f"[Dossier] could not load discovery shortlist: {e}")
            shortlist = []

    built, skipped, errored = [], [], []
    for item in shortlist:
        t = item.get("ticker")
        try:
            path = build_dossier(item, force=force)
            (built if path else skipped).append(t)
        except Exception as e:
            logger.warning(f"[Dossier] {t} failed: {e}")
            errored.append(t)
    logger.info(f"[Dossier] built {len(built)}, skipped {len(skipped)}, errored {len(errored)}")
    return {"built": built, "skipped": skipped, "errored": errored}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(generate_dossiers(), indent=1))
