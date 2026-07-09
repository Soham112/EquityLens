"""
Screener — named pre-filter screens applied before Hunter runs.

Equivalent to screener.in's named screens but for US stocks, built entirely
on yfinance data (free, no API cost). Each screen returns a filtered list of
(ticker, sector) tuples from the input universe.

The screener dramatically reduces how many stocks Hunter and Vision need to
process — from 600 universe tickers down to 20–80 meaningful candidates.

Available screens:
  golden_cross      MA50 recently crossed above MA200 (bullish structure shift)
  breakout          Near 52w high with volume confirmation (momentum)
  multibagger       Strong fundamentals at reasonable valuation (quality growth)
  swing_setup       RSI + MA50 proximity + ATR coiling (technical setup)
  darvas            New high on volume, holding above prior box (momentum)
  oversold_quality  RSI oversold but fundamentals intact (contrarian entry)
  stage2_trend      Weinstein Stage 2 with RS vs SPY (institutional uptrend)

Usage:
  from core.screener import run_screens, SCREENS
  results = run_screens(universe_tickers, screens=["golden_cross", "breakout"])
"""
import logging
import os
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from core.data_layer import (
    FundamentalsData, PriceData,
    fetch_fundamentals, fetch_price_data,
)

logger = logging.getLogger(__name__)


@dataclass
class ScreenCandidate:
    ticker: str
    sector: str
    screens_matched: list[str]       # which screens flagged this ticker
    price: float
    rsi: float
    ma50: float
    ma200: float
    week_52_high_pct: float          # 0 = at 52w high, -0.10 = 10% below
    stage: str                       # Weinstein stage 1-4
    signal_type: str                 # "LONG" or "SWING" — intent for downstream routing
    notes: str                       # human-readable reason summary


# ── Individual screen functions ───────────────────────────────────────────────
# Each takes (price, fundamentals) and returns True if the stock matches.
# Kept as pure functions so they're easy to test and combine.

def _screen_golden_cross(p: PriceData, f: Optional[FundamentalsData]) -> tuple[bool, str]:
    """
    MA50 crossed above MA200 within last 20 trading sessions.
    Uses stage + MA ordering — a true golden cross puts stock in Stage 2.
    Also catches stocks where MA50 just crossed (MACD bullish momentum confirms).
    """
    if p.price_50d_ma <= p.price_200d_ma:
        return False, ""
    # Must be in Stage 2 (price > MA50 > MA200)
    if p.stage not in ("2",):
        return False, ""
    # Golden cross is most powerful when recent — MA50 should be close to MA200
    proximity = (p.price_50d_ma - p.price_200d_ma) / p.price_200d_ma
    if proximity > 0.12:
        return False, ""  # Cross happened long ago — not fresh
    # MACD confirms bullish momentum
    if p.macd_histogram is not None and p.macd_histogram < 0:
        return False, ""
    gap_pct = round(proximity * 100, 1)
    return True, f"Golden cross (MA50 {gap_pct}% above MA200, Stage 2)"


def _screen_breakout(p: PriceData, f: Optional[FundamentalsData]) -> tuple[bool, str]:
    """
    Within 5% of 52-week high with above-average volume.
    Classic momentum breakout — price making new highs on conviction.
    """
    if p.week_52_high_pct is None:
        return False, ""
    if p.week_52_high_pct < -0.05:   # more than 5% below 52w high
        return False, ""
    if p.stage == "4":                # no breakouts in downtrends
        return False, ""
    # RSI should show momentum, not overbought exhaustion
    if p.rsi_14 < 45 or p.rsi_14 > 80:
        return False, ""
    pct_from_high = round(abs(p.week_52_high_pct) * 100, 1)
    return True, f"Breakout setup ({pct_from_high}% from 52w high, RSI {p.rsi_14:.0f})"


def _screen_multibagger(p: PriceData, f: Optional[FundamentalsData]) -> tuple[bool, str]:
    """
    Screener.in multibagger criteria adapted for US stocks:
    ROE > 15%, revenue growth > 10%, P/E reasonable, Stage 2 structure.
    Fundamentals-first — these are long-term hold candidates.
    """
    if f is None:
        return False, ""
    reasons = []

    # Revenue growth > 10% YoY
    rev = f.revenue_growth_yoy
    if rev is None or rev < 0.10:
        return False, ""
    reasons.append(f"rev growth {rev:.0%}")

    # Gross margin > 30% (quality business)
    if f.gross_margin is not None and f.gross_margin < 0.30:
        return False, ""
    if f.gross_margin:
        reasons.append(f"margin {f.gross_margin:.0%}")

    # Not excessively leveraged
    if f.debt_to_equity is not None and f.debt_to_equity > 2.0:
        return False, ""

    # Positive FCF preferred
    if f.fcf is not None and f.fcf < 0:
        return False, ""

    # Price not already in downtrend
    if p.stage == "4":
        return False, ""

    # Earnings consistency
    if f.earnings_beat_rate is not None and f.earnings_beat_rate < 0.50:
        return False, ""

    return True, f"Multibagger criteria — {', '.join(reasons)}"


def _screen_swing_setup(p: PriceData, f: Optional[FundamentalsData]) -> tuple[bool, str]:
    """
    Technical swing setup: RSI in the 'launch zone' (45–62), price near MA50,
    and ATR compression (coiling before expansion).
    Fundamentals irrelevant — this is a pattern-only swing entry.
    """
    # RSI in launch zone — not overbought, not in freefall
    if not (45 <= p.rsi_14 <= 62):
        return False, ""

    # Price within 3% of MA50 — testing support/building base
    if p.price_50d_ma <= 0:
        return False, ""
    proximity_to_ma50 = abs(p.current_price - p.price_50d_ma) / p.price_50d_ma
    if proximity_to_ma50 > 0.03:
        return False, ""

    # ATR compression: volatility coiling = energy building for a move
    if p.atr_compression is not None and p.atr_compression > 0.85:
        return False, ""  # Not compressed enough

    # Must not be in a clear downtrend
    if p.stage == "4":
        return False, ""

    coil = f"ATR compressed {p.atr_compression:.2f}" if p.atr_compression else ""
    return True, f"Swing setup — RSI {p.rsi_14:.0f}, {proximity_to_ma50:.1%} from MA50. {coil}"


def _screen_darvas(p: PriceData, f: Optional[FundamentalsData]) -> tuple[bool, str]:
    """
    Darvas Box: near 52-week high (new territory) + strong relative strength vs SPY.
    Institutions accumulating = stock making new highs while market lags.
    """
    if p.week_52_high_pct is None:
        return False, ""
    if p.week_52_high_pct < -0.08:   # within 8% of 52w high
        return False, ""
    # Must be outperforming the market
    if p.rs_vs_spy is None or p.rs_vs_spy < 0.05:  # >5% better than SPY over 3 months
        return False, ""
    # Stage 2 only
    if p.stage != "2":
        return False, ""

    rs = round(p.rs_vs_spy * 100, 1)
    return True, f"Darvas — {abs(p.week_52_high_pct)*100:.1f}% from 52w high, RS +{rs}% vs SPY"


def _screen_oversold_quality(p: PriceData, f: Optional[FundamentalsData]) -> tuple[bool, str]:
    """
    Contrarian setup: technically oversold (RSI < 35) but fundamentals still intact.
    Catches quality stocks sold off due to market panic, not business deterioration.
    """
    if p.rsi_14 >= 35:
        return False, ""

    # Not a Stage 4 structural downtrend — only temporary pullbacks
    if p.stage == "4" and p.price_50d_ma < p.price_200d_ma * 0.95:
        return False, ""

    if f is None:
        return False, ""

    # Fundamentals must be intact
    rev = f.revenue_growth_yoy
    if rev is not None and rev < -0.05:   # revenue not collapsing
        return False, ""
    if f.fcf is not None and f.fcf < 0:   # still cash-flow positive
        return False, ""

    return True, f"Oversold quality — RSI {p.rsi_14:.0f}, fundamentals intact"


def _screen_stage2_trend(p: PriceData, f: Optional[FundamentalsData]) -> tuple[bool, str]:
    """
    Weinstein Stage 2 with RS > SPY: price above rising MA50 and MA200,
    outperforming the index. Best risk/reward entry zone for trend following.
    """
    if p.stage != "2":
        return False, ""
    if p.rs_vs_spy is None or p.rs_vs_spy < 0.03:
        return False, ""
    if p.rsi_14 > 75:   # not in overbought exhaustion
        return False, ""
    rs = round(p.rs_vs_spy * 100, 1)
    return True, f"Stage 2 trend — RS +{rs}% vs SPY, RSI {p.rsi_14:.0f}"


# ── Screen registry ───────────────────────────────────────────────────────────

@dataclass
class ScreenDef:
    name: str
    fn: Callable
    signal_type: str     # "LONG" or "SWING"
    description: str


SCREENS: dict[str, ScreenDef] = {
    "golden_cross": ScreenDef(
        name="golden_cross",
        fn=_screen_golden_cross,
        signal_type="LONG",
        description="MA50 just crossed above MA200 — structural trend shift",
    ),
    "breakout": ScreenDef(
        name="breakout",
        fn=_screen_breakout,
        signal_type="SWING",
        description="Near 52w high with RSI momentum — institutional accumulation",
    ),
    "multibagger": ScreenDef(
        name="multibagger",
        fn=_screen_multibagger,
        signal_type="LONG",
        description="Strong revenue growth, high margin, positive FCF — quality compounder",
    ),
    "swing_setup": ScreenDef(
        name="swing_setup",
        fn=_screen_swing_setup,
        signal_type="SWING",
        description="RSI launch zone + near MA50 + ATR coiling — technical swing entry",
    ),
    "darvas": ScreenDef(
        name="darvas",
        fn=_screen_darvas,
        signal_type="SWING",
        description="New 52w high territory, outperforming SPY — Darvas box breakout",
    ),
    "oversold_quality": ScreenDef(
        name="oversold_quality",
        fn=_screen_oversold_quality,
        signal_type="LONG",
        description="RSI < 35 but fundamentals intact — panic selloff in quality stock",
    ),
    "stage2_trend": ScreenDef(
        name="stage2_trend",
        fn=_screen_stage2_trend,
        signal_type="LONG",
        description="Weinstein Stage 2, outperforming SPY — clean trend-following entry",
    ),
}


# ── Main entry point ──────────────────────────────────────────────────────────

def _evaluate_ticker(
    ticker: str,
    sector: str,
    screen_names: list[str],
) -> Optional[ScreenCandidate]:
    """
    Fetch data and run all requested screens against a single ticker.
    Returns ScreenCandidate if any screen matches, None otherwise.
    """
    price = fetch_price_data(ticker)
    if price is None:
        return None

    # Fetch fundamentals only if a fundamental screen is requested
    needs_fundamentals = any(
        SCREENS[s].fn.__name__ in (
            "_screen_multibagger", "_screen_oversold_quality", "_screen_golden_cross"
        )
        for s in screen_names if s in SCREENS
    )
    fundamentals = fetch_fundamentals(ticker) if needs_fundamentals else None

    matched = []
    notes_parts = []

    for screen_name in screen_names:
        if screen_name not in SCREENS:
            logger.warning(f"Unknown screen: {screen_name}")
            continue
        screen = SCREENS[screen_name]
        try:
            hit, reason = screen.fn(price, fundamentals)
            if hit:
                matched.append(screen_name)
                notes_parts.append(reason)
        except Exception as e:
            logger.debug(f"[Screener] {screen_name}/{ticker} error: {e}")

    if not matched:
        return None

    # Signal type: if any matched screen is SWING, mark as SWING unless a LONG screen also matched
    long_screens = [s for s in matched if SCREENS[s].signal_type == "LONG"]
    swing_screens = [s for s in matched if SCREENS[s].signal_type == "SWING"]
    if long_screens and swing_screens:
        signal_type = "BOTH"
    elif long_screens:
        signal_type = "LONG"
    else:
        signal_type = "SWING"

    return ScreenCandidate(
        ticker=ticker,
        sector=sector,
        screens_matched=matched,
        price=price.current_price,
        rsi=price.rsi_14,
        ma50=price.price_50d_ma,
        ma200=price.price_200d_ma,
        week_52_high_pct=price.week_52_high_pct or 0.0,
        stage=price.stage or "?",
        signal_type=signal_type,
        notes=" | ".join(notes_parts),
    )


def run_screens(
    universe: list[tuple[str, str]],          # [(ticker, sector), ...]
    screens: Optional[list[str]] = None,       # None = run all screens
    max_workers: int = 8,
    limit: Optional[int] = None,               # cap output size
) -> list[ScreenCandidate]:
    """
    Run named screens against the full universe in parallel.
    Returns candidates sorted by number of screens matched (strongest signals first).

    Args:
        universe:    list of (ticker, sector) tuples
        screens:     list of screen names to run (default: all)
        max_workers: parallel fetch threads
        limit:       max candidates to return

    Returns:
        list of ScreenCandidate, sorted by screens_matched count descending
    """
    screen_names = screens or list(SCREENS.keys())

    # Validate screen names
    unknown = [s for s in screen_names if s not in SCREENS]
    if unknown:
        raise ValueError(f"Unknown screens: {unknown}. Valid: {list(SCREENS.keys())}")

    logger.info(
        f"[Screener] Running {len(screen_names)} screens "
        f"({', '.join(screen_names)}) on {len(universe)} tickers"
    )

    candidates: list[ScreenCandidate] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_evaluate_ticker, ticker, sector, screen_names): ticker
            for ticker, sector in universe
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                candidates.append(result)

    # Sort: most screens matched first, then by RSI (momentum)
    candidates.sort(key=lambda c: (-len(c.screens_matched), -c.rsi))

    if limit:
        candidates = candidates[:limit]

    # Log summary
    swing = [c for c in candidates if c.signal_type in ("SWING", "BOTH")]
    long_ = [c for c in candidates if c.signal_type in ("LONG", "BOTH")]
    logger.info(
        f"[Screener] {len(candidates)} candidates: "
        f"{len(long_)} LONG, {len(swing)} SWING"
    )
    for c in candidates:
        logger.info(
            f"  {c.ticker:<6} [{c.signal_type}] stage={c.stage} "
            f"RSI={c.rsi:.0f} screens={c.screens_matched} — {c.notes}"
        )

    return candidates


def swing_candidates(universe: list[tuple[str, str]], limit: int = 30) -> list[ScreenCandidate]:
    """Convenience: run only swing-oriented screens."""
    return run_screens(
        universe,
        screens=["breakout", "swing_setup", "darvas"],
        limit=limit,
    )


def long_candidates(universe: list[tuple[str, str]], limit: int = 40) -> list[ScreenCandidate]:
    """Convenience: run only long-term-oriented screens."""
    return run_screens(
        universe,
        screens=["golden_cross", "multibagger", "oversold_quality", "stage2_trend"],
        limit=limit,
    )


# ── 7-Signal Swing Momentum Screener ─────────────────────────────────────────

@dataclass
class SwingSignal:
    ticker: str
    sector: str
    microsector: str
    price: float
    signals_fired: list[str]        # which of the 7 fired
    signals_score: int              # 0-7, higher = stronger conviction
    conviction: str                 # HIGH / MEDIUM / LOW
    suggested_dollars: float        # adaptive dollar amount — not a hard cap
    exit_rules: list[str]           # stop_loss | momentum_stall | thesis_break | trail_stop
    notes: str
    # Chart analysis fields — populated after Vision pass (None if not analyzed)
    entry_type: Optional[str] = None           # breakout | pullback | bounce | wait
    pattern: Optional[str] = None             # cup_and_handle | ascending_triangle | etc.
    pattern_confidence: Optional[float] = None
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    stop_level: Optional[float] = None
    target_level: Optional[float] = None
    risk_reward: Optional[float] = None
    support_levels: Optional[list[float]] = None
    resistance_levels: Optional[list[float]] = None
    chart_thesis: Optional[str] = None
    chart_path: Optional[str] = None


def _check_volume_accumulation(p: PriceData) -> tuple[bool, str]:
    """Signal 1: 20d avg volume > 90d avg volume by 20%+ — quiet institutional loading."""
    if p.volume_avg_90d is None or p.volume_avg_90d == 0:
        return False, ""
    ratio = p.volume_avg_20d / p.volume_avg_90d
    if ratio >= 1.20:
        return True, f"Volume accumulation {ratio:.2f}x 90d avg"
    return False, ""


def _check_rs_vs_sector(p: PriceData, sector_etf_return_60d: Optional[float]) -> tuple[bool, str]:
    """Signal 2: stock outperforming its own sector ETF — leading its peers.

    Compares the stock's 3m return against the sector ETF's 60d return
    (close-enough horizons); falls back to RS vs SPY when no ETF return is
    available for the microsector.
    """
    if sector_etf_return_60d is not None and p.return_3m is not None:
        edge = p.return_3m - sector_etf_return_60d
        if edge > 0.03:
            return True, (f"RS vs sector: +{edge*100:.1f}% "
                          f"(stock {p.return_3m*100:+.1f}% vs ETF {sector_etf_return_60d*100:+.1f}%)")
        return False, ""
    # Fallback: no sector ETF data — beat the market instead
    if p.rs_vs_spy is not None and p.rs_vs_spy > 0.05:
        return True, f"RS vs SPY: +{p.rs_vs_spy*100:.1f}% (no sector ETF data)"
    return False, ""


def _check_price_structure(p: PriceData) -> tuple[bool, str]:
    """Signal 3: near 52w high (breakout territory) OR forming a flat base (Stage 1→2 transition)."""
    if p.week_52_high_pct is None:
        return False, ""
    # Near 52w high — breakout territory
    if p.week_52_high_pct >= -0.08 and p.stage in ("2",):
        return True, f"Near 52w high ({abs(p.week_52_high_pct)*100:.1f}% below), Stage 2"
    # Flat base: price near MA50, ATR compressed — energy coiling
    if p.atr_compression is not None and p.atr_compression < 0.75:
        proximity = abs(p.current_price - p.price_50d_ma) / p.price_50d_ma
        if proximity < 0.05:
            return True, f"Flat base forming (ATR compressed {p.atr_compression:.2f}, near MA50)"
    return False, ""


def _check_catalyst_proximity(ticker: str) -> tuple[bool, str]:
    """Signal 4: earnings 14-35 days away — pre-event momentum window."""
    try:
        from core.earnings_calendar import get_next_earnings
        import datetime
        next_date = get_next_earnings(ticker)
        if next_date is None:
            return False, ""
        days_away = (next_date - datetime.date.today()).days
        if 14 <= days_away <= 35:
            return True, f"Earnings in {days_away}d — pre-catalyst window"
    except Exception:
        pass
    return False, ""


def _check_narrative_momentum(f: Optional[FundamentalsData]) -> tuple[bool, str]:
    """Signal 5: positive and improving sentiment from BigData — story building."""
    if f is None:
        return False, ""
    # Beat rate alone is too easy (most of the S&P beats sandbagged estimates).
    # Require consistent beats AND real revenue growth — story backed by numbers.
    beat = f.earnings_beat_rate
    surprise = f.earnings_surprise_avg
    growth = f.revenue_growth_yoy
    if (beat is not None and beat >= 0.75
            and surprise is not None and surprise > 0.05
            and growth is not None and growth > 0.10):
        return True, (f"Beat rate {beat:.0%}, avg surprise +{surprise*100:.1f}%, "
                      f"revenue +{growth*100:.0f}% YoY")
    return False, ""


def _check_insider_buying(ticker: str) -> tuple[bool, str]:
    """Signal 6: net insider buying in open market — executives putting own money in.

    Reads the Sunday sentiment cache (yfinance_insider block) instead of a live
    per-ticker fetch: the old live call cost ~150 HTTP requests per scan day and
    fired 0 times in 522 candidates over 7 days (2026-07-09 audit). Tickers
    outside the weekly cache simply don't fire — same outcome, zero cost.
    """
    try:
        import json
        from core.bigdata_client import CACHE_DIR
        cache_file = CACHE_DIR / f"{ticker.upper()}.json"
        if not cache_file.exists():
            return False, ""
        with open(cache_file) as f:
            ins = json.load(f).get("yfinance_insider") or {}
        if ins.get("net_signal") == "BULLISH" or ins.get("ceo_cfo_buying"):
            return True, "Net insider buying — executives adding shares"
    except Exception:
        pass
    return False, ""


def _check_short_squeeze_potential(p: PriceData) -> tuple[bool, str]:
    """Signal 7: high short interest + stock moving up = squeeze amplifier."""
    if p.short_float_pct is None:
        return False, ""
    if p.short_float_pct >= 0.10 and p.rsi_14 > 50 and p.stage == "2":
        return True, f"Short squeeze setup: {p.short_float_pct*100:.1f}% float short, rising"
    return False, ""


def _adaptive_sizing(
    signals_score: int,
    regime: str = "BULL",
    open_swing_count: int = 0,
    total_swing_capital: float = 1500.0,
    max_concurrent: int = 6,
) -> float:
    """
    Conviction-based position size. No fixed targets or time stops.

    Logic:
      - Max per position = swing_capital / max_concurrent (equal spread if all slots used)
      - Conviction multiplier = signals_score / 7  (scales from 0 to 1)
      - Regime multiplier: BULL=1.0, NEUTRAL=0.7, BEARISH=0.0
      - Output: dollar amount to deploy in this position

    No price targets. No time stops. Exits driven by:
      stop loss | momentum stall | thesis break | trailing stop
    """
    regime_mult = {"BULL": 1.0, "NEUTRAL": 0.7, "BEARISH": 0.0}.get(regime, 1.0)
    if regime_mult == 0.0:
        return 0.0

    # Available slots remaining
    remaining_slots = max(1, max_concurrent - open_swing_count)
    max_per_position = total_swing_capital / remaining_slots

    # Conviction scale: 2/7 signals = 29%, 7/7 = 100%
    conviction_mult = signals_score / 7.0

    dollar_amount = max_per_position * conviction_mult * regime_mult
    return round(dollar_amount, 2)


def _conviction_label(score: int) -> str:
    if score >= 5:
        return "HIGH"
    elif score >= 3:
        return "MEDIUM"
    return "LOW"


def _evaluate_swing_7signal(
    ticker: str,
    sector: str,
    microsector: str,
    sector_etf_return_60d: Optional[float],
    regime: str = "BULL",
    open_swing_count: int = 0,
    total_swing_capital: float = 1500.0,
) -> Optional[SwingSignal]:
    """Run all 7 signals on a single ticker. Returns SwingSignal if ≥2 signals fire."""
    from core.data_layer import fetch_price_data, fetch_fundamentals

    p = fetch_price_data(ticker)
    if p is None:
        return None

    # Skip downtrends and penny stocks
    if p.stage == "4" or p.current_price < 10:
        return None

    f = fetch_fundamentals(ticker)

    fired: list[str] = []
    notes: list[str] = []

    checks = [
        ("volume_accumulation", _check_volume_accumulation(p)),
        ("relative_strength",   _check_rs_vs_sector(p, sector_etf_return_60d)),
        ("price_structure",     _check_price_structure(p)),
        ("catalyst_proximity",  _check_catalyst_proximity(ticker)),
        ("narrative_momentum",  _check_narrative_momentum(f)),
        ("insider_buying",      _check_insider_buying(ticker)),
        ("short_squeeze",       _check_short_squeeze_potential(p)),
    ]

    for signal_name, (hit, reason) in checks:
        if hit:
            fired.append(signal_name)
            notes.append(reason)

    # Catalyst context rule: an earnings date alone is not a signal — the stock
    # must show strength going into it (RS or structure), else it's just the calendar.
    if "catalyst_proximity" in fired:
        if not ({"relative_strength", "price_structure", "volume_accumulation"} & set(fired)):
            idx = fired.index("catalyst_proximity")
            fired.pop(idx)
            notes.pop(idx)

    if len(fired) < 2:
        return None

    score = len(fired)
    suggested = _adaptive_sizing(score, regime, open_swing_count, total_swing_capital)

    return SwingSignal(
        ticker=ticker,
        sector=sector,
        microsector=microsector,
        price=p.current_price,
        signals_fired=fired,
        signals_score=score,
        conviction=_conviction_label(score),
        suggested_dollars=suggested,
        exit_rules=["stop_loss", "momentum_stall", "thesis_break", "trail_stop"],
        notes=" | ".join(notes),
    )


def swing_universe_prefilter(max_tickers: int = 150) -> list[tuple[str, str]]:
    """
    Tier 1 pre-filter: S&P 500 + Nasdaq 100 → math-only rules → ~150 candidates.

    Filters (yfinance data only, no LLM):
      - Price $10-$250 (no penny stocks, no unsizeable mega-priced names)
      - Average volume > 1M shares/day (liquidity, clean entries/exits)
      - RSI between 35 and 75 (not extended, not dead)
      - Price > MA50 (uptrend or recovery)
      - ATR 1.5%-6% of price (controlled volatility band)

    Returns (ticker, microsector) tuples ready for the 7-signal pass.
    Falls back to universe cache if S&P/Nasdaq fetch fails.
    """
    import yfinance as yf
    import numpy as np
    import pandas as pd
    from core.universe import load_universe

    raw_universe = load_universe()
    if not raw_universe:
        logger.warning("[SwingPrefilter] Universe cache empty — run refresh_universe.py first")
        return []

    tickers = [t for t, _ in raw_universe]
    sector_map = {t: s for t, s in raw_universe}
    logger.info(f"[SwingPrefilter] Batch downloading {len(tickers)} stocks...")

    # Single batch download — one request for all tickers, 90 days of daily data.
    # Cached to parquet so charts/S-R/ATR reuse it instead of re-fetching per ticker.
    import datetime as _dt
    cache_path = f"data/ohlcv_cache_{_dt.date.today().isoformat()}.parquet"
    raw = None
    if os.path.exists(cache_path):
        try:
            raw = pd.read_parquet(cache_path)
            logger.info(f"[SwingPrefilter] Loaded OHLCV from cache ({cache_path})")
        except Exception:
            raw = None
    if raw is None:
        try:
            raw = yf.download(
                # 400d (was 90d): charts read this cache and now need MA200 +
                # 52-week-high context; prefilter itself still uses recent tails
                tickers, period="400d", interval="1d",
                progress=False, auto_adjust=True, group_by="ticker",
            )
            try:
                raw.to_parquet(cache_path)
                logger.info(f"[SwingPrefilter] OHLCV cached → {cache_path}")
            except Exception as e:
                logger.debug(f"[SwingPrefilter] parquet cache write failed: {e}")
        except Exception as e:
            logger.warning(f"[SwingPrefilter] Batch download failed: {e}")
            return []

    # Collect ALL passers with a momentum rank, then take the top max_tickers.
    # The old loop broke at max_tickers in universe-cache order, so whoever
    # sorted early in the cache got scanned and later tickers never did.
    passed_ranked: list[tuple[float, str, str]] = []   # (rank_metric, ticker, sector)
    top_level = set(raw.columns.get_level_values(0).unique().tolist()) if isinstance(raw.columns, pd.MultiIndex) else None

    for ticker in tickers:
        try:
            # Extract this ticker's OHLCV from the multi-ticker DataFrame
            if top_level is None:
                df = raw  # single ticker — raw IS the df
            else:
                df = raw[ticker] if ticker in top_level else None
            if df is None or len(df) < 20:
                continue
            df = df.dropna(subset=["Close", "Volume"])
            if len(df) < 20:
                continue

            close = df["Close"]
            volume = df["Volume"]
            high = df["High"]
            low = df["Low"]

            price = float(close.iloc[-1])
            if not (10.0 <= price <= 250.0):
                continue

            avg_vol = float(volume.tail(20).mean())
            if avg_vol < 1_000_000:
                continue

            # RSI(14)
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, float("nan"))
            rsi = float((100 - 100 / (1 + rs)).iloc[-1])
            if not (35 <= rsi <= 75):
                continue

            # Price vs MA50
            if len(close) >= 50:
                ma50 = float(close.tail(50).mean())
                if price < ma50 * 0.97:
                    continue

            # ATR > 0.5% of price
            if len(close) >= 15:
                prev_close = close.shift(1)
                tr = pd.concat([
                    high - low,
                    (high - prev_close).abs(),
                    (low  - prev_close).abs(),
                ], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1])
                # Controlled volatility band: enough range to swing, not so wild stops get blown
                if not (0.015 <= atr / price <= 0.06):
                    continue

            # Rank: 60-day momentum. The 7-signal pass does the real filtering;
            # this just decides who gets a slot when more than max_tickers pass.
            lookback = min(60, len(close) - 1)
            ret_60d = float((close.iloc[-1] - close.iloc[-lookback - 1]) / close.iloc[-lookback - 1])
            passed_ranked.append((ret_60d, ticker, sector_map.get(ticker, "other")))

        except Exception:
            continue

    passed_ranked.sort(key=lambda x: -x[0])
    passed = [(t, s) for _, t, s in passed_ranked[:max_tickers]]
    logger.info(
        f"[SwingPrefilter] {len(passed_ranked)}/{len(tickers)} passed pre-filter"
        + (f" — momentum-ranked to top {max_tickers}" if len(passed_ranked) > max_tickers else "")
    )
    return passed


def _apply_chart_vision(candidates: list["SwingSignal"]) -> None:
    """
    Run chart vision on candidates and populate their entry/stop/R-R fields
    in place. Shared by both signal sources (7-signal screener and Growth
    Hunter) so a candidate's origin doesn't change how its entry is timed.
    """
    if not candidates:
        return
    try:
        from core.swing_chart_analysis import analyze_swing_candidate
        for sig in candidates:
            chart = analyze_swing_candidate(sig.ticker)
            if chart:
                sig.entry_type        = chart.entry_type
                sig.pattern           = chart.pattern
                sig.pattern_confidence = chart.pattern_confidence
                sig.entry_zone_low    = chart.entry_zone_low
                sig.entry_zone_high   = chart.entry_zone_high
                sig.stop_level        = chart.stop_level
                sig.target_level      = chart.target_level
                sig.risk_reward       = chart.risk_reward
                sig.support_levels    = chart.support_levels
                sig.resistance_levels = chart.resistance_levels
                sig.chart_thesis      = chart.chart_thesis
                sig.chart_path        = chart.chart_path
                logger.info(f"  {sig.ticker}: {chart.entry_type} | {chart.pattern} | R/R={chart.risk_reward:.1f}")
    except Exception as e:
        logger.warning(f"[ChartVision] batch failed: {e}")


def growth_hunter_candidates(
    universe: Optional[list[tuple[str, str]]] = None,
    max_workers: int = 5,
) -> list["SwingSignal"]:
    """
    Small/mid-cap Rule-of-40 candidates from agents/growth_hunter.py + the
    curated core/growth_universe.py list — a deliberately different universe
    and scoring philosophy from the 7-signal screener (fundamentals-first,
    $300M-$10B names excluded from the main S&P500/Nasdaq100 scan).

    Returned as SwingSignal objects so both scoring philosophies flow through
    the SAME chart-confirmation and entry gating in auto_enter_swing_signals —
    one portfolio, one set of risk/earnings/sector rules, regardless of which
    scorer sourced the idea. Only SPECULATIVE BUY (score >= 7.0/10) candidates
    are returned; WATCH/PASS are informational only, same as sub-threshold
    results in the main screener.
    """
    from agents import growth_hunter
    from core.growth_universe import get_growth_universe

    universe = universe if universe is not None else get_growth_universe()

    def _one(ticker: str, sector: str) -> Optional[SwingSignal]:
        try:
            price = fetch_price_data(ticker)
            fundamentals = fetch_fundamentals(ticker)
            if price is None or fundamentals is None:
                return None
            r = growth_hunter.run(ticker, sector, price, fundamentals)
            if r.signal != "SPECULATIVE BUY":
                return None
            # Growth Hunter's 0-10 scale isn't the 7-signal screener's 0-7 —
            # rescale so the shared >=4 quality gate in auto_enter still means
            # "this scorer's own high bar was cleared," not a false equivalence.
            score7 = min(7, round(r.score * 0.7))
            return SwingSignal(
                ticker=ticker, sector=sector, microsector=sector,
                price=price.current_price,
                signals_fired=["growth_hunter_rule_of_40"],
                signals_score=score7,
                conviction=_conviction_label(score7),
                suggested_dollars=0.0,  # sizing happens in auto_enter_swing_signals
                exit_rules=["stop_loss", "momentum_stall", "thesis_break", "trail_stop"],
                notes=(f"GrowthHunter {r.score:.1f}/10"
                       f"{f' (Rule of 40={r.rule_of_40:.0f})' if r.rule_of_40 else ''} — "
                       + " | ".join(r.flags[:3])),
            )
        except Exception as e:
            logger.debug(f"[GrowthHunter] {ticker}: {e}")
            return None

    results: list[SwingSignal] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_one, t, s): t for t, s in universe}
        for future in as_completed(futures):
            r = future.result()
            if r:
                results.append(r)

    logger.info(f"[GrowthHunter] {len(results)} SPECULATIVE BUY candidates from {len(universe)} tickers")
    if results:
        _apply_chart_vision(results)
    return results


def swing_momentum_scan(
    universe: list[tuple[str, str]],
    sector_etf_returns: Optional[dict[str, float]] = None,
    min_signals: int = 2,
    max_workers: int = 8,
    regime: str = "BULL",
    total_swing_capital: float = 1500.0,
    run_chart_analysis: bool = True,
) -> list[SwingSignal]:
    """
    7-signal swing momentum screener.

    Args:
        universe:           [(ticker, microsector), ...] — from weekly funnel
        sector_etf_returns: {microsector: 60d_return} from scout — for RS vs sector signal
        min_signals:        minimum signals required to include a stock (default 2)
        max_workers:        parallel threads

    Returns:
        SwingSignal list sorted by signals_score descending (strongest first)
    """
    from core.sector_map import MICRO_SECTORS, MACRO_SECTORS

    # Build microsector → ETF return lookup
    etf_returns = sector_etf_returns or {}

    # Count current open swing positions for sizing context
    open_swing_count = 0
    try:
        from core.position_store import load_swing_positions
        open_swing_count = len(load_swing_positions("OPEN"))
    except Exception:
        pass

    results: list[SwingSignal] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for ticker, microsector in universe:
            macro = MICRO_SECTORS.get(microsector, {}).get("macro", microsector)
            etf_ret = etf_returns.get(microsector) or etf_returns.get(macro)
            futures[pool.submit(
                _evaluate_swing_7signal, ticker, macro, microsector, etf_ret,
                regime, open_swing_count, total_swing_capital,
            )] = ticker

        for future in as_completed(futures):
            result = future.result()
            if result and result.signals_score >= min_signals:
                results.append(result)

    results.sort(key=lambda s: -s.signals_score)

    logger.info(
        f"[SwingScan] {len(results)} candidates from {len(universe)} stocks — "
        f"HIGH={sum(1 for s in results if s.conviction=='HIGH')} "
        f"MEDIUM={sum(1 for s in results if s.conviction=='MEDIUM')} "
        f"LOW={sum(1 for s in results if s.conviction=='LOW')}"
    )
    for s in results[:10]:
        logger.info(
            f"  {s.ticker:<6} [{s.conviction}] {s.signals_score}/7 signals "
            f"| suggested=${s.suggested_dollars:.0f} | {s.notes}"
        )

    # Chart analysis threshold follows the ADAPTIVE signals gate: strict = 4+/7
    # (cost control), exploration with a loose signals gate = 3+/7 — otherwise
    # loose-eligible candidates reach auto-entry chartless and are silently skipped
    # (found live 2026-07-09: MGM 3/7, R/R 3.85, never charted, never entered).
    chart_min = 4
    try:
        from config.settings import settings as _settings
        if getattr(_settings, "swing_entry_mode", "strict") == "exploration":
            from core.feedback import load_gate_state
            if load_gate_state().get("signals") == "loose":
                chart_min = _settings.swing_explore_min_signals
    except Exception as e:
        logger.warning(f"[SwingScan] adaptive chart threshold check failed, using 4+/7: {e}")
    if run_chart_analysis and regime != "BEARISH":
        chart_candidates = [s for s in results if s.signals_score >= chart_min]
        if chart_candidates:
            logger.info(f"[SwingScan] Running chart analysis on {len(chart_candidates)} candidates ({chart_min}+/7 signals)...")
            _apply_chart_vision(chart_candidates)

    return results
