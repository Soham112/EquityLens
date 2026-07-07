"""
Growth Hunter Agent — Scores small/mid-cap growth stocks on a framework
designed for pre-profit or early-profit companies with high asymmetric upside.

Scoring rubric (completely separate from main hunter.py):
  Sector tailwind     30%  — is the parent sector in a structural uptrend?
  Revenue quality     25%  — acceleration, growth rate, Rule of 40
  Gross margin        20%  — quality and expansion trend
  Technical setup     15%  — Stage 2, volume breakout, RS vs sector ETF
  Moat signals        10%  — cash runway, short interest, insider buying proxy

Signal thresholds:
  SPECULATIVE BUY  ≥ 7.0
  WATCH            5.0–6.9
  PASS             < 5.0
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import yfinance as yf
import pandas as pd

from core.data_layer import FundamentalsData, PriceData, fetch_fundamentals, fetch_price_data

logger = logging.getLogger(__name__)

# Sector ETF map — used to check if parent sector is in uptrend
SECTOR_ETF_MAP: dict[str, str] = {
    "semiconductors":     "SMH",
    "ai_infrastructure":  "SMH",
    "technology":         "QQQ",
    "software":           "IGV",
    "biotech":            "XBI",
    "clean_energy":       "ICLN",
    "ev":                 "LIT",
    "cybersecurity":      "CIBR",
    "cloud":              "WCLD",
    "fintech":            "ARKF",
    "genomics":           "ARKG",
    "space":              "UFO",
    "defense":            "ITA",
    "materials":          "XLB",
    "industrials":        "XLI",
}

_sector_cache: dict[str, dict] = {}  # {etf: {date: return_60d}}


@dataclass
class GrowthHunterResult:
    ticker: str
    score: float                      # 0-10
    signal: str                       # SPECULATIVE BUY | WATCH | PASS
    sector_score: float               # 0-3
    revenue_score: float              # 0-2.5
    margin_score: float               # 0-2.0
    technical_score: float            # 0-1.5
    moat_score: float                 # 0-1.0
    rule_of_40: Optional[float]       # revenue_growth% + gross_margin%
    cash_runway_qtrs: Optional[float]
    short_interest_pct: Optional[float]
    flags: list[str] = field(default_factory=list)
    sector_etf: Optional[str] = None
    sector_etf_return_60d: Optional[float] = None


def _get_sector_etf_return(etf: str) -> Optional[float]:
    """60-day return of the sector ETF. Cached per ETF per day."""
    import datetime
    today = datetime.date.today().isoformat()
    if etf in _sector_cache and today in _sector_cache[etf]:
        return _sector_cache[etf][today]
    try:
        hist = yf.Ticker(etf).history(period="90d")
        if hist.empty or len(hist) < 60:
            return None
        ret = float((hist["Close"].iloc[-1] - hist["Close"].iloc[-60]) / hist["Close"].iloc[-60])
        _sector_cache.setdefault(etf, {})[today] = ret
        return ret
    except Exception as e:
        logger.warning(f"Sector ETF {etf} fetch failed: {e}")
        return None


def _score_sector(sector: str) -> tuple[float, list[str], str, Optional[float]]:
    """
    Score 0-3. Checks if the parent sector ETF is in a structural uptrend.
    LEADING (>15% 60d): 3.0 | GAINING (5-15%): 2.0 | FLAT (-5 to 5%): 1.0 | DECLINING (<-5%): 0
    """
    etf = SECTOR_ETF_MAP.get(sector.lower(), "QQQ")
    ret = _get_sector_etf_return(etf)
    flags = []

    if ret is None:
        flags.append(f"Sector ETF {etf}: data unavailable")
        return 1.0, flags, etf, ret

    if ret >= 0.15:
        score = 3.0
        flags.append(f"Sector {etf} LEADING: +{ret:.0%} over 60d — strong structural tailwind")
    elif ret >= 0.05:
        score = 2.0
        flags.append(f"Sector {etf} GAINING: +{ret:.0%} over 60d — moderate tailwind")
    elif ret >= -0.05:
        score = 1.0
        flags.append(f"Sector {etf} FLAT: {ret:+.0%} over 60d — neutral")
    else:
        score = 0.0
        flags.append(f"Sector {etf} DECLINING: {ret:+.0%} over 60d — headwind, pass")

    return score, flags, etf, ret


def _score_revenue(f: FundamentalsData) -> tuple[float, list[str], Optional[float]]:
    """
    Score 0-2.5. Prioritizes acceleration over absolute level.
    Revenue growth YoY: max 1.5 | Acceleration (QoQ trend): max 1.0
    Rule of 40 = revenue_growth% + gross_margin% (returned for display, not scored separately)
    """
    score = 0.0
    flags = []

    # ── Revenue growth YoY (max 1.5) ──
    # Note: very high growth (>200%) from a tiny base is flagged as potential base-effect distortion
    if f.revenue_growth_yoy is not None:
        g = f.revenue_growth_yoy
        if g >= 2.00:
            score += 1.5
            flags.append(f"Revenue growth {g:.0%} YoY — verify base: may reflect early-stage ramp from near-zero, not sustainable acceleration")
        elif g >= 0.50:
            score += 1.5
            flags.append(f"Revenue growth {g:.0%} YoY — exceptional for growth stage")
        elif g >= 0.30:
            score += 1.2
            flags.append(f"Revenue growth {g:.0%} YoY — strong")
        elif g >= 0.15:
            score += 0.7
            flags.append(f"Revenue growth {g:.0%} YoY — moderate, below growth-stage threshold")
        elif g >= 0.0:
            score += 0.2
            flags.append(f"Revenue growth {g:.0%} YoY — weak for a growth pick")
        else:
            flags.append(f"Revenue declining {g:.0%} YoY — fundamental red flag")

    # ── Revenue acceleration QoQ (max 1.0) ──
    if f.revenue_growth_trend == "ACCELERATING":
        score += 1.0
        flags.append("Revenue ACCELERATING QoQ — most important signal for growth stage")
    elif f.revenue_growth_trend == "STABLE":
        score += 0.4
        flags.append("Revenue growth STABLE QoQ")
    elif f.revenue_growth_trend == "DECELERATING":
        flags.append("Revenue DECELERATING QoQ — thesis at risk, monitor closely")

    # Rule of 40 (informational)
    rule_of_40 = None
    if f.revenue_growth_yoy is not None and f.gross_margin is not None:
        rule_of_40 = round((f.revenue_growth_yoy * 100) + (f.gross_margin * 100), 1)

    return min(score, 2.5), flags, rule_of_40


def _score_margins(f: FundamentalsData) -> tuple[float, list[str]]:
    """
    Score 0-2.0. For growth stage companies: gross margin quality + expansion trend.
    FCF not required. Net profit not required.
    """
    score = 0.0
    flags = []

    if f.gross_margin is None:
        flags.append("Gross margin: no data")
        return 0.0, flags

    gm = f.gross_margin
    if gm >= 0.70:
        score += 1.5
        flags.append(f"Gross margin {gm:.0%} — world-class, high pricing power")
    elif gm >= 0.55:
        score += 1.2
        flags.append(f"Gross margin {gm:.0%} — strong for growth stage")
    elif gm >= 0.40:
        score += 0.8
        flags.append(f"Gross margin {gm:.0%} — acceptable, watch for expansion")
    elif gm >= 0.25:
        score += 0.4
        flags.append(f"Gross margin {gm:.0%} — thin, hardware/commodity risk")
    else:
        flags.append(f"Gross margin {gm:.0%} — too thin for asymmetric growth thesis")

    # Margin expansion bonus (max 0.5)
    if f.revenue_growth_trend == "ACCELERATING" and gm >= 0.45:
        score += 0.5
        flags.append("Margin expansion likely as revenue scales — operating leverage thesis intact")

    return min(score, 2.0), flags


def _score_technicals(price: PriceData, sector_etf: str) -> tuple[float, list[str]]:
    """
    Score 0-1.5. Stage 2 + volume breakout + RS vs sector ETF.
    Smaller weight than main hunter because growth stocks are inherently more volatile.
    """
    score = 0.0
    flags = []

    # Stage 2 (0.6 max)
    if hasattr(price, 'stage'):
        if price.stage == "Stage 2":
            score += 0.6
            flags.append("Stage 2 uptrend — price above MA50 above MA200")
        elif price.stage == "Stage 1":
            score += 0.2
            flags.append("Stage 1 basing — potential Stage 2 setup forming")
        elif price.stage == "Stage 4":
            flags.append("Stage 4 downtrend — avoid, wait for base to form")

    # Dollar volume liquidity check (0.3) — use avg 20d volume × price as proxy
    daily_dollar_vol = price.volume_avg_20d * price.current_price
    if daily_dollar_vol >= 5_000_000:
        score += 0.3
        flags.append(f"Avg daily volume ${daily_dollar_vol/1e6:.1f}M — sufficient liquidity for entry/exit")
    elif daily_dollar_vol >= 1_000_000:
        score += 0.1
        flags.append(f"Avg daily volume ${daily_dollar_vol/1e6:.1f}M — thin, size carefully")

    # MACD bullish crossover (0.3)
    if hasattr(price, 'macd_cross_bullish') and price.macd_cross_bullish:
        score += 0.3
        flags.append("MACD bullish crossover — momentum turning positive")

    # RS vs sector ETF (0.3)
    if hasattr(price, 'rs_vs_spy') and price.rs_vs_spy is not None:
        # For growth stocks, RS vs sector is more meaningful than vs SPY
        # Use RS vs SPY as proxy (positive = outperforming broadly)
        if price.rs_vs_spy >= 0.10:
            score += 0.3
            flags.append(f"RS vs SPY +{price.rs_vs_spy:.0%} — outperforming broad market")
        elif price.rs_vs_spy >= 0.0:
            score += 0.1
            flags.append(f"RS vs SPY +{price.rs_vs_spy:.0%} — slight outperformance")

    return min(score, 1.5), flags


def _score_moat(ticker: str, f: FundamentalsData, price: PriceData) -> tuple[float, list[str], Optional[float], Optional[float]]:
    """
    Score 0-1.0. Cash runway, short interest, insider buying proxy (net insider transactions).
    These are the survivability and conviction signals for small-caps.
    """
    score = 0.0
    flags = []
    cash_runway_qtrs = None
    short_interest_pct = None

    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}

        # Cash runway (quarters of operation at current burn rate)
        total_cash = info.get("totalCash")
        operating_cashflow = info.get("operatingCashflow")
        if total_cash and operating_cashflow and operating_cashflow < 0:
            quarterly_burn = abs(operating_cashflow) / 4
            cash_runway_qtrs = round(total_cash / quarterly_burn, 1)
            if cash_runway_qtrs >= 8:
                score += 0.4
                flags.append(f"Cash runway {cash_runway_qtrs:.0f}Q — well-funded, no dilution risk near term")
            elif cash_runway_qtrs >= 4:
                score += 0.2
                flags.append(f"Cash runway {cash_runway_qtrs:.0f}Q — adequate but watch for capital raise")
            else:
                flags.append(f"Cash runway {cash_runway_qtrs:.0f}Q — dilution risk within 12 months")
        elif total_cash and (not operating_cashflow or operating_cashflow >= 0):
            score += 0.4
            flags.append("Cash flow positive or breakeven — no runway risk")

        # Short interest (lower is better; >25% is a risk flag)
        short_pct = info.get("shortPercentOfFloat")
        if short_pct is not None:
            short_interest_pct = round(short_pct * 100, 1)
            if short_pct <= 0.10:
                score += 0.3
                flags.append(f"Short interest {short_interest_pct:.1f}% — low, market not betting against")
            elif short_pct <= 0.20:
                score += 0.15
                flags.append(f"Short interest {short_interest_pct:.1f}% — moderate")
            elif short_pct <= 0.30:
                flags.append(f"Short interest {short_interest_pct:.1f}% — elevated, real skepticism or squeeze potential")
            else:
                flags.append(f"Short interest {short_interest_pct:.1f}% — very high, significant risk")

        # Institutional ownership increasing (proxy: >50% owned is good for small-cap)
        inst_pct = info.get("institutionalOwnershipPercent") or info.get("heldPercentInstitutions")
        if inst_pct is not None:
            if inst_pct >= 0.50:
                score += 0.3
                flags.append(f"Institutional ownership {inst_pct:.0%} — smart money present")
            elif inst_pct >= 0.25:
                score += 0.1
                flags.append(f"Institutional ownership {inst_pct:.0%} — moderate coverage")
            else:
                flags.append(f"Institutional ownership {inst_pct:.0%} — low, undiscovered or avoided")

    except Exception as e:
        logger.debug(f"{ticker}: moat data fetch failed: {e}")

    return min(score, 1.0), flags, cash_runway_qtrs, short_interest_pct


def run(ticker: str, sector: str, price: PriceData, fundamentals: FundamentalsData) -> GrowthHunterResult:
    """Score a growth stock. Returns GrowthHunterResult with signal."""

    sector_score, sector_flags, sector_etf, etf_return = _score_sector(sector)
    revenue_score, revenue_flags, rule_of_40 = _score_revenue(fundamentals)
    margin_score, margin_flags = _score_margins(fundamentals)
    tech_score, tech_flags = _score_technicals(price, sector_etf or "QQQ")
    moat_score, moat_flags, cash_runway, short_interest = _score_moat(ticker, fundamentals, price)

    total = sector_score + revenue_score + margin_score + tech_score + moat_score
    # Normalize to 0-10 (max raw = 3 + 2.5 + 2 + 1.5 + 1 = 10)
    score = round(min(total, 10.0), 2)

    if score >= 7.0:
        signal = "SPECULATIVE BUY"
    elif score >= 5.0:
        signal = "WATCH"
    else:
        signal = "PASS"

    all_flags = sector_flags + revenue_flags + margin_flags + tech_flags + moat_flags

    logger.info(f"{ticker}: Growth score={score:.1f} ({signal}) | "
                f"Sector={sector_score:.1f} Rev={revenue_score:.1f} "
                f"Margin={margin_score:.1f} Tech={tech_score:.1f} Moat={moat_score:.1f}")

    return GrowthHunterResult(
        ticker=ticker,
        score=score,
        signal=signal,
        sector_score=sector_score,
        revenue_score=revenue_score,
        margin_score=margin_score,
        technical_score=tech_score,
        moat_score=moat_score,
        rule_of_40=rule_of_40,
        cash_runway_qtrs=cash_runway,
        short_interest_pct=short_interest,
        flags=all_flags,
        sector_etf=sector_etf,
        sector_etf_return_60d=etf_return,
    )
