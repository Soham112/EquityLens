"""
YFinance Sentiment — drop-in replacement for BigData MCP.

Writes to data/bigdata_cache/{ticker}.json in the same format as BigData cache,
so all downstream code (bigdata_client.py, agents) works unchanged.

Sources:
  yfinance.info               → fundamentals, analyst consensus, price targets
  yfinance.news               → headlines for Haiku sentiment analysis
  yfinance.earnings_history   → EPS actual vs estimated, surprise %
  yfinance.insider_transactions → CEO/CFO buy/sell (SEC Form 4)
  Claude Haiku                → sentiment score, risk flags, narrative

Cost: ~$0.03 per weekly full refresh (58 tickers × ~15 headlines × Haiku pricing)
"""
import json
import logging
import time
from datetime import date, datetime
from typing import Optional

import yfinance as yf

from core.bigdata_client import save_cache
from core.llm_client import HAIKU, call_llm

logger = logging.getLogger(__name__)

MAX_HEADLINES = 15

_HAIKU_SYSTEM = (
    "You are a financial news analyst. "
    "Analyze stock news headlines and return JSON only — no explanation, no markdown fences."
)


# ── Data fetchers ──────────────────────────────────────────────────────────────

def _fetch_info(ticker: str) -> dict:
    try:
        return yf.Ticker(ticker).info or {}
    except Exception as e:
        logger.warning(f"{ticker}: yfinance info failed: {e}")
        return {}


def _fetch_headlines(ticker: str) -> list[str]:
    try:
        news = yf.Ticker(ticker).news or []
        out = []
        for item in news[:MAX_HEADLINES]:
            # yfinance returns two shapes depending on version
            title = (
                item.get("title")
                or (item.get("content") or {}).get("title")
                or ""
            )
            if title:
                out.append(title.strip())
        return out
    except Exception as e:
        logger.warning(f"{ticker}: news fetch failed: {e}")
        return []


def _fetch_earnings(ticker: str) -> dict:
    """Latest quarter EPS actual vs estimated from yfinance earnings_history."""
    try:
        hist = yf.Ticker(ticker).earnings_history
        if hist is None or hist.empty:
            return {}
        row = hist.iloc[0]
        eps_actual    = float(row.get("epsActual", 0) or 0)
        eps_estimated = float(row.get("epsEstimate", 0) or 0)
        surprise      = float(row.get("surprisePercent", 0) or 0)
        # yfinance returns surprise as a decimal (0.06 = 6%) on some versions, whole number on others
        if abs(surprise) < 5:
            surprise = surprise * 100
        return {
            "eps_actual":      round(eps_actual, 4),
            "eps_estimated":   round(eps_estimated, 4),
            "eps_surprise_pct": round(surprise, 2),
        }
    except Exception as e:
        logger.warning(f"{ticker}: earnings history failed: {e}")
        return {}


def _fetch_insider(ticker: str) -> dict:
    """Net insider signal from SEC Form 4 data via yfinance."""
    default = {"net_signal": "NEUTRAL", "ceo_cfo_buying": False, "ceo_cfo_selling": False}
    try:
        txns = yf.Ticker(ticker).insider_transactions
        if txns is None or txns.empty:
            return default

        buying = selling = False
        for _, row in txns.head(20).iterrows():
            text     = str(row.get("Text", "") or row.get("Transaction", "")).lower()
            position = str(row.get("Position", "")).lower()
            shares   = float(row.get("Shares", 0) or 0)
            is_exec  = any(p in position for p in ["ceo", "cfo", "chief", "president", "director"])
            if not is_exec or shares <= 0:
                continue
            if any(w in text for w in ("purchase", "acqui", " buy")):
                buying = True
            elif any(w in text for w in ("sale", "sell", "disposit")):
                selling = True

        net = (
            "BULLISH" if buying and not selling
            else "BEARISH" if selling and not buying
            else "NEUTRAL"
        )
        return {"net_signal": net, "ceo_cfo_buying": buying, "ceo_cfo_selling": selling}
    except Exception as e:
        logger.warning(f"{ticker}: insider transactions failed: {e}")
        return default


# ── Haiku sentiment pass ───────────────────────────────────────────────────────

def _run_haiku_sentiment(ticker: str, headlines: list[str], company_name: str) -> dict:
    """Run Claude Haiku on news headlines → sentiment score + risk flags + narrative."""
    _empty = {
        "sentiment_score": 0.0,
        "sentiment_direction": "neutral",
        "litigation_flag": False,
        "sec_investigation_flag": False,
        "auditor_warning_flag": False,
        "export_control_flag": False,
        "key_events": [],
        "narrative": "No recent news available.",
    }
    if not headlines:
        return _empty

    headlines_text = "\n".join(f"- {h}" for h in headlines)
    prompt = f"""Analyze these recent news headlines for {ticker} ({company_name}):

{headlines_text}

Return valid JSON with exactly these fields:
{{
  "sentiment_score": <float -1.0 to 1.0>,
  "sentiment_direction": "<bullish|bearish|neutral>",
  "litigation_flag": <true/false — lawsuit, class action, legal dispute>,
  "sec_investigation_flag": <true/false — SEC probe, investigation, enforcement>,
  "auditor_warning_flag": <true/false — going concern, auditor resignation, restatement>,
  "export_control_flag": <true/false — export controls, sanctions, trade restrictions>,
  "key_events": [<short labels e.g. "earnings_beat", "guidance_cut", "contract_win", "insider_buying">],
  "narrative": "<2 sentence summary of the main sentiment drivers>"
}}"""

    raw = call_llm(system=_HAIKU_SYSTEM, user=prompt, model=HAIKU, max_tokens=400)
    if not raw:
        return _empty

    try:
        text = raw.strip()
        # Strip markdown fences if Haiku wraps anyway
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logger.warning(f"{ticker}: Haiku JSON parse failed ({e}) — raw: {raw[:120]}")
        return _empty


# ── Analyst breakdown ──────────────────────────────────────────────────────────

def _analyst_breakdown(info: dict) -> dict:
    """
    Approximate analyst buy/hold/sell counts.
    yfinance provides recommendationMean (1=Strong Buy … 5=Strong Sell)
    and numberOfAnalystOpinions but not individual category counts.
    """
    n    = int(info.get("numberOfAnalystOpinions", 0) or 0)
    key  = (info.get("recommendationKey") or "hold").lower().replace("_", " ")
    mean = float(info.get("recommendationMean", 3.0) or 3.0)

    consensus_map = {
        "strong buy": "Strong Buy", "buy": "Buy",
        "hold": "Hold", "underperform": "Sell",
        "sell": "Sell", "strong sell": "Strong Sell",
    }
    consensus = consensus_map.get(key, "Hold")

    # Distribute across bins based on mean score
    if mean <= 1.5:
        buy_frac, hold_frac, sell_frac = 0.85, 0.12, 0.03
    elif mean <= 2.0:
        buy_frac, hold_frac, sell_frac = 0.70, 0.22, 0.08
    elif mean <= 2.5:
        buy_frac, hold_frac, sell_frac = 0.55, 0.32, 0.13
    elif mean <= 3.0:
        buy_frac, hold_frac, sell_frac = 0.35, 0.45, 0.20
    elif mean <= 3.5:
        buy_frac, hold_frac, sell_frac = 0.20, 0.45, 0.35
    elif mean <= 4.0:
        buy_frac, hold_frac, sell_frac = 0.10, 0.30, 0.60
    else:
        buy_frac, hold_frac, sell_frac = 0.05, 0.15, 0.80

    buy  = round(n * buy_frac)
    hold = round(n * hold_frac)
    sell = n - buy - hold  # remainder avoids rounding drift

    return {
        "strong_buy":   buy // 2,
        "buy":          buy - buy // 2,
        "hold":         max(0, hold),
        "sell":         max(0, sell // 2),
        "strong_sell":  max(0, sell - sell // 2),
        "consensus":    consensus,
    }


# ── Cache builder ──────────────────────────────────────────────────────────────

def _build_cache_entry(
    ticker: str,
    info: dict,
    earnings: dict,
    insider: dict,
    sentiment: dict,
    headlines: list[str],
) -> dict:
    """
    Assemble cache dict in the exact same format as BigData MCP output.
    parse_sentiment(), parse_fundamentals(), parse_insider_flags() all read this unchanged.
    """
    analyst = _analyst_breakdown(info)
    company_name = info.get("longName") or info.get("shortName") or ticker
    s = float(sentiment.get("sentiment_score", 0.0))

    # Encode structured flags into docs/narrative so the keyword scanner
    # in parse_insider_flags() still works as a fallback.
    docs = [{"headline": h} for h in headlines]
    flag_headlines = {
        "litigation_flag":       "lawsuit litigation class action legal dispute filed",
        "sec_investigation_flag":"sec investigation probe enforcement action",
        "auditor_warning_flag":  "going concern auditor warning restatement",
        "export_control_flag":   "export control restriction sanction",
    }
    for flag, synthetic in flag_headlines.items():
        if sentiment.get(flag):
            docs.append({"headline": synthetic})

    insider_text = ""
    if insider.get("ceo_cfo_buying"):
        insider_text += " insider buy: executive purchased shares."
    if insider.get("ceo_cfo_selling"):
        insider_text += " insider sell: executive sold shares."

    narrative = str(sentiment.get("narrative", "")) + insider_text

    quarter_label = f"Q{((date.today().month - 1) // 3) + 1} {date.today().year}"

    return {
        "ticker":     ticker,
        "entity_id":  f"yf_{ticker}",
        "fetched_at": datetime.now().isoformat(),
        "source":     "yfinance+haiku",
        "company": {
            "name":     company_name,
            "ticker":   ticker,
            "sector":   info.get("sector", ""),
            "industry": info.get("industry", ""),
        },
        # ── Sentiment section ── (read by parse_sentiment + parse_insider_flags)
        "sentiment": {
            "signals": {
                "sentiment": {
                    "current":    s,
                    "baseline":   0.0,
                    "momentum":   s,      # use current score as directional proxy
                    "zscore_1mo": 0.0,    # no historical baseline available
                    "zscore_1qt": 0.0,
                },
                "media_attention": {
                    # Proxy: more headlines = higher attention
                    "momentum_pct": min(len(headlines) * 8.0, 100.0),
                },
            },
            "evidence": {"docs": docs},
            "narrative": narrative,
        },
        # ── Tearsheet section ── (read by parse_fundamentals)
        "tearsheet": {
            "company_overview": {
                "ticker":     ticker,
                "market_cap": info.get("marketCap"),
            },
            "key_financial_highlights": {
                "gross_profit_margin_ttm": info.get("grossMargins"),
                "net_profit_margin_ttm":   info.get("profitMargins"),
                "pe_ratio_ttm":            info.get("trailingPE"),
                "period":                  quarter_label,
            },
            "analyst_data": {
                "ratings": analyst,
                "price_targets": {
                    "target_consensus": info.get("targetMeanPrice"),
                    "target_median":    info.get("targetMedianPrice"),
                },
            },
            "latest_earnings": {
                "revenue": {
                    "actual":       None,   # yfinance doesn't provide rev vs estimate
                    "estimated":    None,
                    "surprise_pct": None,
                },
                "eps": {
                    "actual":       earnings.get("eps_actual"),
                    "estimated":    earnings.get("eps_estimated"),
                    "surprise_pct": earnings.get("eps_surprise_pct"),
                },
            },
        },
        # ── Structured insider section ── (read by updated get_insider_flags)
        "yfinance_insider": {
            "net_signal":      insider.get("net_signal", "NEUTRAL"),
            "ceo_cfo_buying":  insider.get("ceo_cfo_buying", False),
            "ceo_cfo_selling": insider.get("ceo_cfo_selling", False),
        },
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def refresh_ticker(ticker: str) -> bool:
    """Full yfinance+Haiku refresh for one ticker. Returns True on success."""
    logger.info(f"[YFSentiment] {ticker} — fetching...")
    try:
        info      = _fetch_info(ticker)
        if not info:
            logger.warning(f"[YFSentiment] {ticker}: no yfinance info — skipping")
            return False

        company   = info.get("longName") or info.get("shortName") or ticker
        headlines = _fetch_headlines(ticker)
        earnings  = _fetch_earnings(ticker)
        insider   = _fetch_insider(ticker)
        sentiment = _run_haiku_sentiment(ticker, headlines, company)

        cache = _build_cache_entry(ticker, info, earnings, insider, sentiment, headlines)
        save_cache(ticker, cache)

        logger.info(
            f"[YFSentiment] {ticker} saved — "
            f"sentiment={sentiment.get('sentiment_score', 0):.2f} "
            f"({sentiment.get('sentiment_direction', 'neutral')}) | "
            f"headlines={len(headlines)} | insider={insider.get('net_signal')} | "
            f"flags: lit={sentiment.get('litigation_flag')} "
            f"sec={sentiment.get('sec_investigation_flag')}"
        )
        return True
    except Exception as e:
        logger.error(f"[YFSentiment] {ticker} failed: {e}")
        return False


def refresh_all(tickers: list[str], delay_sec: float = 0.3) -> dict:
    """
    Refresh all tickers sequentially with a small delay.
    Returns {"success": [...], "failed": [...], "total": int}
    """
    success, failed = [], []
    for i, ticker in enumerate(tickers, 1):
        logger.info(f"[YFSentiment] {i}/{len(tickers)}: {ticker}")
        ok = refresh_ticker(ticker)
        (success if ok else failed).append(ticker)
        if delay_sec and i < len(tickers):
            time.sleep(delay_sec)

    logger.info(
        f"[YFSentiment] Complete: {len(success)}/{len(tickers)} ok"
        + (f" | Failed: {failed}" if failed else "")
    )
    return {"success": success, "failed": failed, "total": len(tickers)}
