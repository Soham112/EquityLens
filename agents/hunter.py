"""
Hunter Agent — Score stocks 0-10 based on fundamentals, technicals, valuation.
0-3: Skip | 4-6: Watchlist | 7+: Research candidate
"""
import logging
from dataclasses import dataclass
from typing import Optional

from core.data_layer import FundamentalsData, PriceData

logger = logging.getLogger(__name__)


@dataclass
class HunterResult:
    ticker: str
    score: float        # 0-10
    fundamentals_score: float
    technicals_score: float
    valuation_score: float
    flags: list[str]    # reasons score moved up/down
    recommendation: str  # "SKIP" | "WATCHLIST" | "RESEARCH_CANDIDATE"


def _score_fundamentals(f: FundamentalsData) -> tuple[float, list[str]]:
    score = 0.0
    flags = []

    # ── Revenue growth YoY (max 2.5) ──
    if f.revenue_growth_yoy is not None:
        if f.revenue_growth_yoy >= 0.25:
            score += 2.5
            flags.append(f"Revenue growth {f.revenue_growth_yoy:.0%} YoY — strong")
        elif f.revenue_growth_yoy >= 0.15:
            score += 1.5
            flags.append(f"Revenue growth {f.revenue_growth_yoy:.0%} YoY — good")
        elif f.revenue_growth_yoy >= 0.05:
            score += 0.75
            flags.append(f"Revenue growth {f.revenue_growth_yoy:.0%} YoY — moderate")
        else:
            flags.append(f"Revenue growth {f.revenue_growth_yoy:.0%} YoY — weak")
    else:
        flags.append("Revenue growth YoY: missing")

    # ── Revenue trend: accelerating adds bonus, decelerating penalizes (max ±1.0) ──
    if f.revenue_growth_trend:
        if f.revenue_growth_trend == "ACCELERATING":
            score += 1.0
            q = [f"{g:+.0%}" for g in (f.quarterly_rev_growth or [])]
            flags.append(f"Revenue accelerating QoQ {' → '.join(q)} — momentum building")
        elif f.revenue_growth_trend == "DECELERATING":
            score -= 0.5
            q = [f"{g:+.0%}" for g in (f.quarterly_rev_growth or [])]
            flags.append(f"Revenue decelerating QoQ {' → '.join(q)} — growth fading")
        else:
            flags.append("Revenue growth stable QoQ")

    # ── Earnings beat track record (max 2.0) ──
    if f.earnings_beat_rate is not None:
        surprise_str = f", avg surprise {f.earnings_surprise_avg:+.0%}" if f.earnings_surprise_avg is not None else ""
        if f.earnings_beat_rate >= 0.75:
            score += 2.0
            flags.append(f"Beat EPS estimates {f.earnings_beat_rate:.0%} of last 4 quarters{surprise_str} — reliable execution")
        elif f.earnings_beat_rate >= 0.50:
            score += 1.0
            flags.append(f"Beat EPS estimates {f.earnings_beat_rate:.0%} of last 4 quarters{surprise_str}")
        else:
            score -= 0.5
            flags.append(f"Only beat EPS {f.earnings_beat_rate:.0%} of last 4 quarters{surprise_str} — inconsistent execution")

    # ── FCF (max 2.0) ──
    if f.fcf is not None:
        if f.fcf > 0:
            score += 2.0
            flags.append("FCF positive — self-funding")
        else:
            flags.append("FCF negative — cash burn risk")
    else:
        flags.append("FCF: missing")

    # ── Gross margin (max 2.5) ──
    if f.gross_margin is not None:
        if f.gross_margin >= 0.60:
            score += 2.5
            flags.append(f"Gross margin {f.gross_margin:.0%} — exceptional pricing power")
        elif f.gross_margin >= 0.50:
            score += 2.0
            flags.append(f"Gross margin {f.gross_margin:.0%} — excellent")
        elif f.gross_margin >= 0.35:
            score += 1.0
            flags.append(f"Gross margin {f.gross_margin:.0%} — good")
        elif f.gross_margin >= 0.20:
            score += 0.5
            flags.append(f"Gross margin {f.gross_margin:.0%} — moderate")
        else:
            flags.append(f"Gross margin {f.gross_margin:.0%} — thin margins")
    else:
        flags.append("Gross margin: missing")

    return round(min(score, 10.0), 1), flags


def _score_technicals(p: PriceData) -> tuple[float, list[str]]:
    score = 0.0
    flags = []

    # ── RSI (max 2.0) ──
    if 40 <= p.rsi_14 <= 70:
        score += 2.0
        flags.append(f"RSI {p.rsi_14:.0f} — healthy range")
    elif 30 <= p.rsi_14 < 40:
        score += 1.5
        flags.append(f"RSI {p.rsi_14:.0f} — slightly oversold, possible entry")
    elif p.rsi_14 > 70:
        score += 0.5
        flags.append(f"RSI {p.rsi_14:.0f} — overbought, caution")
    else:
        score += 0.5
        flags.append(f"RSI {p.rsi_14:.0f} — oversold")

    # ── MA50 / MA200 trend structure (max 2.0) ──
    above_50 = p.current_price > p.price_50d_ma
    above_200 = p.current_price > p.price_200d_ma
    golden_cross = p.price_50d_ma > p.price_200d_ma
    if above_50 and above_200 and golden_cross:
        score += 2.0
        flags.append(f"Golden cross — price above both MAs (50d={p.price_50d_ma:.0f}, 200d={p.price_200d_ma:.0f})")
    elif above_50 and above_200:
        score += 1.5
        flags.append(f"Price above 50d and 200d MA")
    elif above_50:
        score += 1.0
        flags.append(f"Price above 50d MA — 200d MA not yet cleared")
    else:
        flags.append(f"Price below 50d MA ({p.price_50d_ma:.0f}) — weak trend")

    # ── Volume liquidity (max 2.0) ──
    daily_dollar_vol = p.volume_avg_20d * p.current_price
    if daily_dollar_vol >= 20e6:
        score += 2.0
        flags.append("Volume >$20M daily — liquid")
    elif daily_dollar_vol >= 5e6:
        score += 1.0
        flags.append("Volume $5–20M daily — moderate liquidity")
    else:
        flags.append("Volume <$5M — illiquid, risk")

    # ── MACD momentum (max 2.0) ──
    if p.macd_histogram is not None:
        if p.macd_cross_bullish:
            score += 2.0
            flags.append(f"MACD bullish crossover (hist={p.macd_histogram:+.3f}) — fresh momentum signal")
        elif p.macd_histogram > 0:
            score += 1.0
            flags.append(f"MACD histogram positive ({p.macd_histogram:+.3f}) — uptrend momentum")
        else:
            flags.append(f"MACD histogram negative ({p.macd_histogram:+.3f}) — momentum fading")

    # ── Relative strength vs SPY (max 1.5) ──
    if p.rs_vs_spy is not None:
        if p.rs_vs_spy >= 0.10:
            score += 1.5
            flags.append(f"RS vs SPY +{p.rs_vs_spy:.0%} (3mo) — strong outperformance")
        elif p.rs_vs_spy >= 0.03:
            score += 1.0
            flags.append(f"RS vs SPY +{p.rs_vs_spy:.0%} (3mo) — outperforming market")
        elif p.rs_vs_spy >= -0.03:
            score += 0.5
            flags.append(f"RS vs SPY {p.rs_vs_spy:+.0%} (3mo) — in line with market")
        else:
            flags.append(f"RS vs SPY {p.rs_vs_spy:+.0%} (3mo) — underperforming market")

    # ── 52-week high proximity (max 0.5) ──
    if p.week_52_high_pct is not None:
        if p.week_52_high_pct >= -0.05:
            score += 0.5
            flags.append(f"Within 5% of 52-week high — institutional accumulation zone")
        elif p.week_52_high_pct >= -0.15:
            score += 0.25
            flags.append(f"{p.week_52_high_pct:.0%} below 52-week high")
        else:
            flags.append(f"{p.week_52_high_pct:.0%} below 52-week high — well off peak")

    # ── Weinstein Stage (max 1.5, penalty for Stage 3/4) ──
    if p.stage:
        if p.stage == "2":
            score += 1.5
            flags.append("Stage 2 uptrend — ideal entry zone (price > MA50 > MA200)")
        elif p.stage == "1":
            score += 0.5
            flags.append("Stage 1 basing — potential early entry, not confirmed yet")
        elif p.stage == "3":
            score -= 0.5
            flags.append("Stage 3 topping — distribution risk, avoid new entries")
        elif p.stage == "4":
            score -= 1.0
            flags.append("Stage 4 downtrend — do not buy, wait for Stage 1 base")

    # ── ATR compression (coiling for breakout) (max 0.5) ──
    if p.atr_compression is not None:
        if p.atr_compression <= 0.60:
            score += 0.5
            flags.append(f"ATR compression {p.atr_compression:.2f} — tight coil, breakout likely imminent")
        elif p.atr_compression <= 0.75:
            score += 0.25
            flags.append(f"ATR compression {p.atr_compression:.2f} — volatility contracting")

    return round(min(score, 10.0), 1), flags


def _score_valuation(f: FundamentalsData) -> tuple[float, list[str]]:
    score = 5.0  # neutral start
    flags = []

    if f.pe_ratio is not None:
        if f.pe_ratio < 15:
            score += 2.0
            flags.append(f"P/E {f.pe_ratio:.1f} — cheap")
        elif f.pe_ratio < 30:
            score += 0.0
            flags.append(f"P/E {f.pe_ratio:.1f} — fair")
        elif f.pe_ratio < 60:
            score -= 1.5
            flags.append(f"P/E {f.pe_ratio:.1f} — elevated")
        else:
            score -= 3.0
            flags.append(f"P/E {f.pe_ratio:.1f} — very high, justify with growth")
    else:
        flags.append("P/E: missing")

    if f.peg_ratio is not None:
        if f.peg_ratio < 1.0:
            score += 2.0
            flags.append(f"PEG {f.peg_ratio:.2f} — undervalued vs growth")
        elif f.peg_ratio < 2.0:
            score += 0.5
            flags.append(f"PEG {f.peg_ratio:.2f} — reasonable")
        else:
            score -= 1.0
            flags.append(f"PEG {f.peg_ratio:.2f} — expensive vs growth")
    else:
        flags.append("PEG: missing")

    return round(max(0, min(10, score)), 1), flags


def run(price: PriceData, fundamentals: FundamentalsData) -> HunterResult:
    ticker = price.ticker

    # Liquidity gate
    if price.market_cap < 100e6:
        return HunterResult(
            ticker=ticker, score=0, fundamentals_score=0,
            technicals_score=0, valuation_score=0,
            flags=["Market cap <$100M — below liquidity minimum"],
            recommendation="SKIP",
        )

    fund_score, fund_flags = _score_fundamentals(fundamentals)
    tech_score, tech_flags = _score_technicals(price)
    val_score, val_flags = _score_valuation(fundamentals)

    # Adaptive weights from signal_tracker (falls back to base weights if insufficient data)
    try:
        from core.signal_tracker import get_adaptive_weights
        weights = get_adaptive_weights()
        w_fund = weights.get("fundamentals", 0.50)
        w_tech = weights.get("technicals", 0.30)
        w_val = weights.get("valuation", 0.20)
    except Exception:
        w_fund, w_tech, w_val = 0.50, 0.30, 0.20

    # Weighted average: fundamentals 50%, technicals 30%, valuation 20% (adaptive)
    total = (fund_score * w_fund) + (tech_score * w_tech) + (val_score * w_val)
    total = round(min(total, 10.0), 1)

    if total >= 7:
        rec = "RESEARCH_CANDIDATE"
    elif total >= 4:
        rec = "WATCHLIST"
    else:
        rec = "SKIP"

    all_flags = fund_flags + tech_flags + val_flags

    return HunterResult(
        ticker=ticker,
        score=total,
        fundamentals_score=round(fund_score, 1),
        technicals_score=round(tech_score, 1),
        valuation_score=val_score,
        flags=all_flags,
        recommendation=rec,
    )
