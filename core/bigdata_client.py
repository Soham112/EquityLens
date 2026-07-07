"""
BigData.com client — parses cached tearsheet/sentiment JSON into internal types.

Architecture:
  - BigData MCP tools are called by Claude (see workflows/bigdata_refresh.py)
  - Results are cached to data/bigdata_cache/{ticker}.json
  - This module reads that cache and maps it to our internal data structures
  - yfinance handles price/ATR/RSI (real-time, standalone)

Data source: Bigdata.com (https://bigdata.com) — powered by RavenPack
"""
import datetime
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.data_layer import FundamentalsData, InsiderData

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/bigdata_cache")


# ── Internal types returned by this module ──

@dataclass
class BigDataSentiment:
    ticker: str
    sentiment_score: float          # -1.0 to +1.0 (current)
    sentiment_baseline: float
    sentiment_momentum: float       # positive = improving
    media_attention_momentum: float # % change
    zscore_1mo: float
    zscore_1qt: float
    narrative: str                  # AI-generated summary
    events: list[str]               # detected events from news
    source_count: int
    requires_human_review: bool
    as_of: str


@dataclass
class BigDataFundamentals:
    ticker: str
    market_cap: Optional[float]
    gross_margin: Optional[float]
    net_margin: Optional[float]
    pe_ratio: Optional[float]
    revenue_actual: Optional[float]
    revenue_estimated: Optional[float]
    revenue_surprise_pct: Optional[float]
    eps_actual: Optional[float]
    eps_estimated: Optional[float]
    eps_surprise_pct: Optional[float]
    analyst_buy: int
    analyst_hold: int
    analyst_sell: int
    analyst_consensus: str          # "Buy" | "Hold" | "Sell"
    price_target_consensus: Optional[float]
    price_target_median: Optional[float]
    as_of: str


@dataclass
class BigDataInsider:
    """Derived from news/regulatory filings in BigData sentiment docs."""
    ticker: str
    ceo_cfo_selling: bool
    ceo_cfo_buying: bool
    net_insider_signal: str   # "BULLISH" | "BEARISH" | "NEUTRAL"
    export_control_flag: bool
    litigation_flag: bool
    sec_investigation_flag: bool
    auditor_warning_flag: bool
    as_of: str


# ── Cache I/O ──

def cache_path(ticker: str) -> Path:
    return CACHE_DIR / f"{ticker.upper()}.json"


def load_cache(ticker: str) -> Optional[dict]:
    path = cache_path(ticker)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"load_cache({ticker}): {e}")
        return None


def save_cache(ticker: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path(ticker), "w") as f:
        json.dump(data, f, indent=2)
    logger.info(f"BigData cache saved: {ticker}")


def cache_age_days(ticker: str) -> Optional[int]:
    path = cache_path(ticker)
    if not path.exists():
        return None
    mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
    return (datetime.datetime.now() - mtime).days


# ── Parsers ──

def parse_sentiment(ticker: str, raw: dict) -> Optional[BigDataSentiment]:
    try:
        signals = raw.get("signals", {})
        sent = signals.get("sentiment", {})
        media = signals.get("media_attention", {})
        docs = raw.get("evidence", {}).get("docs", [])
        narrative = raw.get("narrative", "")

        # Detect risk events from document headlines
        events = []
        risk_keywords = {
            "earnings beat": "earnings_beat",
            "beat estimates": "earnings_beat",
            "guidance cut": "guidance_cut",
            "lowered guidance": "guidance_cut",
            "contract win": "contract_win",
            "strategic collaboration": "contract_win",
            "lawsuit": "lawsuit",
            "litigation": "litigation",
            "sec investigation": "sec_investigation",
            "export control": "export_control",
            "insider buy": "insider_buying",
            "insider sell": "insider_selling",
            "regulatory approval": "regulatory_approval",
            "resigned": "executive_resignation",
        }
        all_headlines = " ".join(d.get("headline", "").lower() for d in docs)
        for kw, event in risk_keywords.items():
            if kw in all_headlines and event not in events:
                events.append(event)

        current = sent.get("current", 0.0) or 0.0
        requires_review = abs(current) > 0.5

        return BigDataSentiment(
            ticker=ticker,
            sentiment_score=current,
            sentiment_baseline=sent.get("baseline", 0.0) or 0.0,
            sentiment_momentum=sent.get("momentum", 0.0) or 0.0,
            media_attention_momentum=media.get("momentum_pct", 0.0) or 0.0,
            zscore_1mo=sent.get("zscore_1mo", 0.0) or 0.0,
            zscore_1qt=sent.get("zscore_1qt", 0.0) or 0.0,
            narrative=narrative[:2000] if narrative else "",
            events=events,
            source_count=len(docs),
            requires_human_review=requires_review,
            as_of=raw.get("company", {}).get("ticker", ticker),
        )
    except Exception as e:
        logger.error(f"parse_sentiment({ticker}): {e}")
        return None


def parse_fundamentals(ticker: str, raw: dict) -> Optional[BigDataFundamentals]:
    try:
        kf = raw.get("key_financial_highlights", {})
        ad = raw.get("analyst_data", {})
        le = raw.get("latest_earnings", {})
        co = raw.get("company_overview", {})

        ratings = ad.get("ratings", {})
        pt = ad.get("price_targets", {})

        buy = (ratings.get("strong_buy", 0) or 0) + (ratings.get("buy", 0) or 0)
        hold = ratings.get("hold", 0) or 0
        sell = (ratings.get("sell", 0) or 0) + (ratings.get("strong_sell", 0) or 0)

        return BigDataFundamentals(
            ticker=ticker,
            market_cap=co.get("market_cap"),
            gross_margin=kf.get("gross_profit_margin_ttm"),
            net_margin=kf.get("net_profit_margin_ttm"),
            pe_ratio=kf.get("pe_ratio_ttm"),
            revenue_actual=le.get("revenue", {}).get("actual"),
            revenue_estimated=le.get("revenue", {}).get("estimated"),
            revenue_surprise_pct=le.get("revenue", {}).get("surprise_pct"),
            eps_actual=le.get("eps", {}).get("actual"),
            eps_estimated=le.get("eps", {}).get("estimated"),
            eps_surprise_pct=le.get("eps", {}).get("surprise_pct"),
            analyst_buy=buy,
            analyst_hold=hold,
            analyst_sell=sell,
            analyst_consensus=ratings.get("consensus", "Hold"),
            price_target_consensus=pt.get("target_consensus"),
            price_target_median=pt.get("target_median"),
            as_of=kf.get("period", ""),
        )
    except Exception as e:
        logger.error(f"parse_fundamentals({ticker}): {e}")
        return None


def parse_insider_flags(ticker: str, sentiment_raw: dict) -> BigDataInsider:
    """
    Derive insider/risk flags from BigData sentiment document headlines.
    These feed directly into the Critic agent.
    """
    docs = sentiment_raw.get("evidence", {}).get("docs", [])
    narrative = (sentiment_raw.get("narrative", "") or "").lower()
    all_text = narrative + " ".join(d.get("headline", "").lower() for d in docs)

    return BigDataInsider(
        ticker=ticker,
        ceo_cfo_selling="insider sell" in all_text or "ceo sell" in all_text,
        ceo_cfo_buying="insider buy" in all_text or "ceo buy" in all_text,
        net_insider_signal=(
            "BULLISH" if "insider buy" in all_text and "insider sell" not in all_text
            else "BEARISH" if "insider sell" in all_text
            else "NEUTRAL"
        ),
        export_control_flag="export control" in all_text or "export restriction" in all_text,
        litigation_flag="lawsuit" in all_text or "litigation" in all_text or "class action" in all_text,
        sec_investigation_flag="sec investigation" in all_text or "sec probe" in all_text,
        auditor_warning_flag="going concern" in all_text or "auditor warning" in all_text,
        as_of=datetime.date.today().isoformat(),
    )


# ── Public API used by data_layer + agents ──

def get_sentiment(ticker: str) -> Optional[BigDataSentiment]:
    cache = load_cache(ticker)
    if not cache or "sentiment" not in cache:
        logger.warning(f"{ticker}: no BigData sentiment cache — run bigdata_refresh.py")
        return None
    return parse_sentiment(ticker, cache["sentiment"])


def get_fundamentals(ticker: str) -> Optional[BigDataFundamentals]:
    cache = load_cache(ticker)
    if not cache or "tearsheet" not in cache:
        logger.warning(f"{ticker}: no BigData tearsheet cache — run bigdata_refresh.py")
        return None
    return parse_fundamentals(ticker, cache["tearsheet"])


def get_insider_flags(ticker: str) -> Optional[BigDataInsider]:
    cache = load_cache(ticker)
    if not cache:
        return None
    # Always keyword-scan sentiment docs for kill-switch flags (litigation, SEC, auditor)
    risk_flags = parse_insider_flags(ticker, cache.get("sentiment", {})) if "sentiment" in cache else None

    # Prefer structured yfinance_insider for insider buy/sell (SEC Form 4 — more reliable than keywords)
    yf_ins = cache.get("yfinance_insider")
    if yf_ins:
        return BigDataInsider(
            ticker=ticker,
            ceo_cfo_selling=yf_ins.get("ceo_cfo_selling", False),
            ceo_cfo_buying=yf_ins.get("ceo_cfo_buying", False),
            net_insider_signal=yf_ins.get("net_signal", "NEUTRAL"),
            export_control_flag=risk_flags.export_control_flag if risk_flags else False,
            litigation_flag=risk_flags.litigation_flag if risk_flags else False,
            sec_investigation_flag=risk_flags.sec_investigation_flag if risk_flags else False,
            auditor_warning_flag=risk_flags.auditor_warning_flag if risk_flags else False,
            as_of=cache.get("fetched_at", datetime.date.today().isoformat()),
        )
    # Fallback: keyword-scan only (original BigData path)
    return risk_flags


def to_fundamentals_data(ticker: str, bd: BigDataFundamentals) -> FundamentalsData:
    """Convert BigDataFundamentals → internal FundamentalsData for Hunter agent."""
    return FundamentalsData(
        ticker=ticker,
        revenue_growth_yoy=None,         # BigData gives surprise%, not YoY — yfinance fills this
        fcf=None,                         # filled by yfinance
        gross_margin=bd.gross_margin,
        debt_to_equity=None,              # filled by yfinance
        pe_ratio=bd.pe_ratio,
        peg_ratio=None,
        ev_to_fcf=None,
        last_earnings_date=None,
        last_updated=datetime.date.today(),
    )


def to_insider_data(ticker: str, bd: BigDataInsider) -> InsiderData:
    """Convert BigDataInsider → internal InsiderData for Critic agent."""
    return InsiderData(
        ticker=ticker,
        ceo_cfo_selling=bd.ceo_cfo_selling,
        ceo_cfo_buying=bd.ceo_cfo_buying,
        net_insider_signal=bd.net_insider_signal,
        last_updated=datetime.date.today(),
    )
