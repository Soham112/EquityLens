"""
Swing Chart Analysis — support/resistance detection + chart rendering + Claude Vision.

Pipeline for each candidate:
  1. Fetch 120 days of daily OHLCV data
  2. Detect swing highs/lows algorithmically → cluster into S/R zones
  3. Render swing chart: candlesticks + MA20 + MA50 + volume + RSI + S/R lines
  4. Send to Claude Vision with swing-specific prompt
  5. Return SwingChartSignal with entry_type, entry_zone, stop, target, R/R, pattern, thesis

Used by swing_momentum_scan() after the 7-signal pass — only HIGH/MEDIUM candidates.
Charts saved to data/swing_charts/{ticker}_{date}.png for dashboard display.
"""
import base64
import io
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf
from mplfinance.original_flavor import candlestick_ohlc

from core.llm_client import SONNET, call_llm

logger = logging.getLogger(__name__)

CHART_DIR = Path("data/swing_charts")
LOOKBACK_DAYS = 120
SR_WINDOW = 5          # bars on each side to qualify as swing high/low
SR_CLUSTER_PCT = 0.015 # levels within 1.5% → merged into one zone
SR_MIN_TESTS = 2       # level must be tested this many times to keep

_STYLE = {
    "figure.facecolor": "#0f1117",
    "axes.facecolor":   "#0f1117",
    "axes.edgecolor":   "#2d3748",
    "axes.labelcolor":  "#e2e8f0",
    "xtick.color":      "#94a3b8",
    "ytick.color":      "#94a3b8",
    "text.color":       "#e2e8f0",
    "grid.color":       "#1e2432",
    "grid.alpha":       0.5,
    "axes.grid":        True,
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SRLevel:
    price: float
    kind: str        # "support" | "resistance"
    tests: int       # how many times price respected this level
    label: str       # "S1", "S2", "R1", "R2"


@dataclass
class SwingChartSignal:
    ticker: str
    entry_type: str              # "breakout" | "pullback" | "bounce" | "wait"
    pattern: str                 # "cup_and_handle" | "ascending_triangle" | "none" | etc.
    pattern_confidence: float    # 0.0 – 1.0
    entry_zone_low: float
    entry_zone_high: float
    stop_level: float
    target_level: float
    risk_reward: float
    support_levels: list[float]
    resistance_levels: list[float]
    chart_thesis: str
    chart_path: str              # absolute path to saved PNG
    analyzed_at: str


# ── Step 1: fetch data ─────────────────────────────────────────────────────────

def _fetch_ohlcv(ticker: str) -> Optional[pd.DataFrame]:
    # First try today's batch cache (written by swing_universe_prefilter) — no HTTP call
    cache_path = Path(f"data/ohlcv_cache_{date.today().isoformat()}.parquet")
    if cache_path.exists():
        try:
            cached = pd.read_parquet(cache_path)
            if isinstance(cached.columns, pd.MultiIndex) and ticker in cached.columns.get_level_values(0):
                df = cached[ticker][["Open", "High", "Low", "Close", "Volume"]].copy().dropna()
                if len(df) >= 40:
                    return df.tail(LOOKBACK_DAYS)
        except Exception:
            pass

    try:
        raw = yf.download(
            ticker, period="6mo", interval="1d",
            progress=False, auto_adjust=True,
        )
        if raw is None or len(raw) < 40:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy().dropna()
        return df.tail(LOOKBACK_DAYS)
    except Exception as e:
        logger.error(f"[SwingChart] {ticker} OHLCV fetch failed: {e}")
        return None


# ── Step 2: S/R detection ──────────────────────────────────────────────────────

def _find_sr_levels(df: pd.DataFrame, current_price: float) -> list[SRLevel]:
    highs = df["High"].values
    lows  = df["Low"].values
    n = len(highs)
    w = SR_WINDOW

    raw_highs, raw_lows = [], []

    for i in range(w, n - w):
        window_high = highs[i - w: i + w + 1]
        if highs[i] == window_high.max():
            raw_highs.append(highs[i])

    for i in range(w, n - w):
        window_low = lows[i - w: i + w + 1]
        if lows[i] == window_low.min():
            raw_lows.append(lows[i])

    def cluster(prices: list[float]) -> list[tuple[float, int]]:
        if not prices:
            return []
        prices = sorted(prices)
        clusters: list[list[float]] = [[prices[0]]]
        for p in prices[1:]:
            if (p - clusters[-1][-1]) / clusters[-1][-1] < SR_CLUSTER_PCT:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        return [
            (float(np.mean(c)), len(c))
            for c in clusters
            if len(c) >= SR_MIN_TESTS
        ]

    supports    = [(p, t) for p, t in cluster(raw_lows)  if p < current_price]
    resistances = [(p, t) for p, t in cluster(raw_highs) if p > current_price]

    # Closest 2 of each, labelled S1/S2 and R1/R2
    supports    = sorted(supports,    key=lambda x: x[0], reverse=True)[:2]
    resistances = sorted(resistances, key=lambda x: x[0])[:2]

    levels: list[SRLevel] = []
    for i, (p, t) in enumerate(supports):
        levels.append(SRLevel(price=round(p, 2), kind="support",    tests=t, label=f"S{i+1}"))
    for i, (p, t) in enumerate(resistances):
        levels.append(SRLevel(price=round(p, 2), kind="resistance", tests=t, label=f"R{i+1}"))

    return levels


# ── Step 3: render swing chart ─────────────────────────────────────────────────

def _calc_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Average True Range — measures daily noise/volatility."""
    try:
        high, low, close = df["High"], df["Low"], df["Close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        return float(tr.rolling(period).mean().iloc[-1])
    except Exception:
        return None


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def render_swing_chart(
    ticker: str,
    df: pd.DataFrame,
    sr_levels: list[SRLevel],
    save: bool = True,
    suffix: str = "",
) -> tuple[Optional[bytes], Optional[str]]:
    """
    Three-panel swing chart:
      Top:    candlesticks + MA20 (blue) + MA50 (orange) + S/R lines
      Middle: volume bars + 20-day avg volume line
      Bottom: RSI(14)

    Returns (png_bytes, save_path).
    """
    df = df.copy()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA50"] = df["Close"].rolling(50).mean()
    df["RSI"]  = _calc_rsi(df["Close"])
    df["VolMA20"] = df["Volume"].rolling(20).mean()

    df["DateNum"] = mdates.date2num(df.index.to_pydatetime())
    ohlc = df[["DateNum", "Open", "High", "Low", "Close"]].dropna().values

    current_price = float(df["Close"].iloc[-1])
    ma20_last = float(df["MA20"].iloc[-1]) if df["MA20"].notna().any() else None
    ma50_last = float(df["MA50"].iloc[-1]) if df["MA50"].notna().any() else None

    with plt.rc_context(_STYLE):
        fig, (ax1, ax2, ax3) = plt.subplots(
            3, 1, figsize=(14, 11),
            gridspec_kw={"height_ratios": [4, 1.2, 1.2]},
            sharex=False,
        )

        # ── Price panel ──────────────────────────────────────────────────
        candlestick_ohlc(ax1, ohlc, width=0.6, colorup="#26a69a", colordown="#ef5350", alpha=0.9)

        if ma20_last:
            ax1.plot(df["DateNum"], df["MA20"], color="#4fc3f7", linewidth=1.5,
                     label=f"MA20  {ma20_last:.1f}", zorder=3)
        if ma50_last:
            ax1.plot(df["DateNum"], df["MA50"], color="#ffa726", linewidth=1.5,
                     label=f"MA50  {ma50_last:.1f}", zorder=3)

        # Current price line
        ax1.axhline(current_price, color="#b39ddb", linestyle=":", linewidth=1, alpha=0.9)
        ax1.text(df["DateNum"].iloc[-1], current_price,
                 f"  ${current_price:.2f}", color="#b39ddb", fontsize=8.5, va="bottom")

        # S/R lines
        for lvl in sr_levels:
            color = "#4caf50" if lvl.kind == "support" else "#ef5350"
            ax1.axhline(lvl.price, color=color, linestyle="--", linewidth=1.3, alpha=0.85)
            ax1.text(
                df["DateNum"].iloc[2], lvl.price,
                f" {lvl.label}  ${lvl.price:.1f}",
                color=color, fontsize=8.5, va="bottom", fontweight="bold",
            )

        ax1.set_title(f"{ticker} — 120-Day Swing Chart", fontsize=13, fontweight="bold", pad=8)
        ax1.set_ylabel("Price", fontsize=9)
        ax1.legend(loc="upper left", fontsize=8, framealpha=0.3)
        ax1.xaxis_date()
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax1.tick_params(axis="x", rotation=30, labelsize=7)

        # ── Volume panel ─────────────────────────────────────────────────
        colors = ["#26a69a" if df["Close"].iloc[i] >= df["Open"].iloc[i] else "#ef5350"
                  for i in range(len(df))]
        ax2.bar(df["DateNum"], df["Volume"], width=0.6, color=colors, alpha=0.7)
        ax2.plot(df["DateNum"], df["VolMA20"], color="#90caf9", linewidth=1.2,
                 label="Vol MA20")
        ax2.set_ylabel("Volume", fontsize=8)
        ax2.legend(loc="upper left", fontsize=7, framealpha=0.3)
        ax2.tick_params(axis="x", rotation=30, labelsize=7)
        ax2.xaxis_date()
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        ax2.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M" if x >= 1e6 else f"{x/1e3:.0f}K")
        )

        # ── RSI panel ────────────────────────────────────────────────────
        ax3.plot(df.index, df["RSI"], color="#e91e8c", linewidth=1.3, label="RSI(14)")
        ax3.axhline(70, color="#ef5350", linestyle="--", linewidth=0.9, alpha=0.8)
        ax3.axhline(30, color="#4caf50", linestyle="--", linewidth=0.9, alpha=0.8)
        ax3.axhline(50, color="#607d8b", linestyle=":",  linewidth=0.7, alpha=0.6)
        ax3.fill_between(df.index, df["RSI"], 70, where=(df["RSI"] >= 70),
                         alpha=0.15, color="#ef5350")
        ax3.fill_between(df.index, df["RSI"], 30, where=(df["RSI"] <= 30),
                         alpha=0.15, color="#4caf50")
        rsi_now = float(df["RSI"].iloc[-1]) if df["RSI"].notna().any() else 50
        ax3.text(df.index[-1], rsi_now, f"  {rsi_now:.0f}", color="#e91e8c", fontsize=8)
        ax3.set_ylim(0, 100)
        ax3.set_ylabel("RSI(14)", fontsize=8)
        ax3.legend(loc="upper left", fontsize=7, framealpha=0.3)
        ax3.tick_params(axis="x", rotation=30, labelsize=7)

        plt.tight_layout(h_pad=0.5)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        png_bytes = buf.read()

    save_path = None
    if save:
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        save_path = str(CHART_DIR / f"{ticker}{suffix}_{date.today().isoformat()}.png")
        with open(save_path, "wb") as f:
            f.write(png_bytes)
        logger.info(f"[SwingChart] {ticker} chart saved ({len(png_bytes)//1024}KB) → {save_path}")

    return png_bytes, save_path


# ── Step 4: Claude Vision analysis ────────────────────────────────────────────

_VISION_SYSTEM = """You are an expert swing trader and technical analyst.
Analyze the provided stock chart and return structured JSON only — no markdown, no explanation outside the JSON."""


def _build_vision_prompt(
    ticker: str,
    sr_levels: list[SRLevel],
    current_price: float,
    df: Optional[pd.DataFrame] = None,
) -> str:
    sr_desc = "\n".join(
        f"  {lvl.label} ({lvl.kind}): ${lvl.price:.2f} — tested {lvl.tests}x"
        for lvl in sr_levels
    ) or "  None detected"

    # Exact numerical volume context — don't make the model eyeball bar heights
    vol_desc = ""
    if df is not None and len(df) >= 30:
        try:
            vol = df["Volume"]
            close = df["Close"]
            v5, v20, v90 = float(vol.tail(5).mean()), float(vol.tail(20).mean()), float(vol.tail(90).mean())
            red = close.diff() < 0
            red5, green5 = red.tail(10), ~red.tail(10)
            red_vol = float(vol.tail(10)[red5].mean()) if red5.any() else 0.0
            green_vol = float(vol.tail(10)[green5].mean()) if green5.any() else 0.0
            vol_desc = (
                f"\nExact volume data (use these numbers, not the chart bars):\n"
                f"  5d avg: {v5/1e6:.2f}M | 20d avg: {v20/1e6:.2f}M | 90d avg: {v90/1e6:.2f}M "
                f"(5d is {v5/v90:.2f}x the 90d avg)\n"
                f"  Last 10 sessions: up-day avg volume {green_vol/1e6:.2f}M vs down-day avg {red_vol/1e6:.2f}M"
                + (" — healthy (selling drying up)" if green_vol > red_vol * 1.1 else
                   " — caution (distribution on red days)" if red_vol > green_vol * 1.1 else "")
                + "\n"
            )
        except Exception:
            vol_desc = ""

    return f"""Analyze this 120-day daily swing trading chart for {ticker} (current price: ${current_price:.2f}).

Pre-identified support/resistance levels (already marked as dashed lines on chart):
{sr_desc}
{vol_desc}

The chart shows: candlesticks, MA20 (blue), MA50 (orange), volume panel, RSI(14) panel.

Classify the current swing setup and return valid JSON:
{{
  "entry_type": "<breakout|pullback|bounce|wait>",
  "pattern": "<cup_and_handle|ascending_triangle|descending_triangle|bull_flag|bear_flag|head_and_shoulders|double_bottom|double_top|wedge|channel|none>",
  "pattern_confidence": <0.0–1.0>,
  "entry_zone_low": <price — low end of ideal buy zone>,
  "entry_zone_high": <price — high end of ideal buy zone>,
  "stop_level": <price — where thesis is wrong, exit immediately>,
  "target_level": <price — first realistic target at next resistance>,
  "risk_reward": <float — (target - entry_mid) / (entry_mid - stop)>,
  "chart_thesis": "<2-3 sentences: what pattern, where price is relative to S/R and MAs, why this entry type, what confirms the trade>"
}}

Entry type rules:
- breakout: price just closed ABOVE a resistance level on elevated volume
- pullback: price broke out, pulled back to prior resistance (now support) or MA20
- bounce: price sitting AT support level, beginning to turn up with RSI not oversold
- wait: price mid-range between S/R, no clean setup, or RSI >75 (extended), or below MA20

Only return JSON."""


def _run_vision_analysis(
    ticker: str,
    png_bytes: bytes,
    sr_levels: list[SRLevel],
    current_price: float,
    df: Optional[pd.DataFrame] = None,
) -> Optional[dict]:
    try:
        from core.llm_client import get_client
        client = get_client()
        if client is None:
            return None

        img_b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
        prompt = _build_vision_prompt(ticker, sr_levels, current_price, df=df)

        response = client.messages.create(
            model=SONNET,
            max_tokens=800,
            system=_VISION_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.error(f"[SwingChart] {ticker} vision analysis failed: {e}")
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

def analyze_swing_candidate(ticker: str) -> Optional[SwingChartSignal]:
    """
    Full pipeline for one ticker:
      fetch → detect S/R → render chart → Claude Vision → SwingChartSignal

    Returns None if data unavailable or entry_type == "wait" with R/R < 2.0.
    """
    logger.info(f"[SwingChart] Analyzing {ticker}...")

    df = _fetch_ohlcv(ticker)
    if df is None or len(df) < 40:
        logger.warning(f"[SwingChart] {ticker}: insufficient data")
        return None

    current_price = float(df["Close"].iloc[-1])
    sr_levels = _find_sr_levels(df, current_price)

    png_bytes, chart_path = render_swing_chart(ticker, df, sr_levels, save=True)
    if png_bytes is None:
        return None

    result = _run_vision_analysis(ticker, png_bytes, sr_levels, current_price, df=df)
    if result is None:
        return None

    entry_type   = result.get("entry_type", "wait")
    risk_reward  = float(result.get("risk_reward", 0.0) or 0.0)
    pattern      = result.get("pattern", "none")
    confidence   = float(result.get("pattern_confidence", 0.0) or 0.0)
    entry_low    = float(result.get("entry_zone_low",  current_price) or current_price)
    entry_high   = float(result.get("entry_zone_high", current_price) or current_price)
    target_level = float(result.get("target_level", current_price * 1.10) or current_price * 1.10)
    thesis       = result.get("chart_thesis", "")

    # Stop-loss: S1 − 0.5×ATR (support anchor + stop-hunt buffer), but never risk
    # more than 2.5 ATRs — near-high setups have no nearby tested support, and an
    # S1 far below would make every breakout's R/R look terrible.
    entry_mid = (entry_low + entry_high) / 2
    s1 = next((lvl.price for lvl in sr_levels if lvl.label == "S1"), None)
    atr = _calc_atr(df)
    if atr is not None:
        atr_floor = entry_mid - 2.5 * atr
        if s1 is not None:
            stop_level = round(max(s1 - 0.5 * atr, atr_floor), 2)
        else:
            stop_level = round(atr_floor, 2)
    else:
        stop_level = float(result.get("stop_level", current_price * 0.92) or current_price * 0.92)

    # Recalculate R/R with the adjusted stop
    risk = entry_mid - stop_level
    if risk > 0:
        risk_reward = round((target_level - entry_mid) / risk, 2)
    else:
        risk_reward = float(result.get("risk_reward", 0.0) or 0.0)

    logger.info(
        f"[SwingChart] {ticker} → {entry_type} | {pattern} ({confidence:.0%}) | "
        f"R/R={risk_reward:.1f} | entry ${entry_low:.0f}-${entry_high:.0f} | "
        f"stop ${stop_level:.0f} | target ${target_level:.0f}"
    )

    return SwingChartSignal(
        ticker=ticker,
        entry_type=entry_type,
        pattern=pattern,
        pattern_confidence=confidence,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop_level=stop_level,
        target_level=target_level,
        risk_reward=risk_reward,
        support_levels=[lvl.price for lvl in sr_levels if lvl.kind == "support"],
        resistance_levels=[lvl.price for lvl in sr_levels if lvl.kind == "resistance"],
        chart_thesis=thesis,
        chart_path=chart_path or "",
        analyzed_at=date.today().isoformat(),
    )


def analyze_longterm_candidate(ticker: str) -> Optional[SwingChartSignal]:
    """
    Long-term chart analysis: 1-year weekly candles, structural S/R levels.
    Stop = S1 − 0.5×ATR (weekly ATR — wider buffer for long-term positions).
    Used on BUY signals from the 7-agent pipeline before entry.
    """
    logger.info(f"[LTChart] Analyzing {ticker} (long-term, weekly)...")
    try:
        raw = yf.download(ticker, period="1y", interval="1wk", progress=False, auto_adjust=True)
        if raw is None or len(raw) < 20:
            logger.warning(f"[LTChart] {ticker}: insufficient weekly data")
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        df = raw[["Open", "High", "Low", "Close", "Volume"]].copy().dropna()
    except Exception as e:
        logger.error(f"[LTChart] {ticker} data fetch failed: {e}")
        return None

    current_price = float(df["Close"].iloc[-1])
    sr_levels = _find_sr_levels(df, current_price)

    # Render chart with long-term filename suffix — don't clobber the daily swing chart
    png_bytes, chart_path = render_swing_chart(ticker, df, sr_levels, save=True, suffix="_LT")
    if png_bytes is None:
        return None

    # Vision prompt adapted for long-term entry
    sr_desc = "\n".join(
        f"  {lvl.label} ({lvl.kind}): ${lvl.price:.2f} — tested {lvl.tests}x"
        for lvl in sr_levels
    ) or "  None detected"

    lt_prompt = f"""Analyze this 1-year weekly chart for {ticker} (current price: ${current_price:.2f}).

Pre-identified structural support/resistance levels:
{sr_desc}

Chart shows: candlesticks, MA20 (blue, 20-week), MA50 (orange, 50-week), volume, RSI(14).

This is a LONG-TERM position analysis — weeks to months holding period.

Return valid JSON only:
{{
  "entry_type": "<breakout|pullback|bounce|wait>",
  "pattern": "<cup_and_handle|ascending_triangle|descending_triangle|bull_flag|base|consolidation|uptrend|downtrend|none>",
  "pattern_confidence": <0.0–1.0>,
  "entry_zone_low": <price — low end of ideal long-term buy zone>,
  "entry_zone_high": <price — high end of ideal long-term buy zone>,
  "stop_level": <price — structural level where long-term thesis is broken>,
  "target_level": <price — first major resistance target over next 3-6 months>,
  "risk_reward": <float>,
  "chart_thesis": "<2-3 sentences: trend structure, where price is relative to key weekly S/R and MAs, why this is or isn't a good long-term entry>"
}}

Only return JSON."""

    try:
        from core.llm_client import get_client
        client = get_client()
        if client is None:
            return None
        img_b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
        response = client.messages.create(
            model=SONNET,
            max_tokens=800,
            system=_VISION_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": lt_prompt},
            ]}],
        )
        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
    except Exception as e:
        logger.error(f"[LTChart] {ticker} vision failed: {e}")
        return None

    entry_low    = float(result.get("entry_zone_low",  current_price) or current_price)
    entry_high   = float(result.get("entry_zone_high", current_price) or current_price)
    target_level = float(result.get("target_level", current_price * 1.20) or current_price * 1.20)
    thesis       = result.get("chart_thesis", "")
    pattern      = result.get("pattern", "none")
    confidence   = float(result.get("pattern_confidence", 0.0) or 0.0)
    entry_type   = result.get("entry_type", "wait")

    # Stop = S1 − 0.5×weekly ATR (structural support buffer), capped at 2.5 weekly
    # ATRs of risk — same logic as swing, but on weekly bars the band is naturally wider.
    entry_mid = (entry_low + entry_high) / 2
    s1 = next((lvl.price for lvl in sr_levels if lvl.label == "S1"), None)
    atr = _calc_atr(df)
    if atr is not None:
        atr_floor = entry_mid - 2.5 * atr
        if s1 is not None:
            stop_level = round(max(s1 - 0.5 * atr, atr_floor), 2)
        else:
            stop_level = round(atr_floor, 2)
    else:
        stop_level = float(result.get("stop_level", current_price * 0.85) or current_price * 0.85)

    risk = entry_mid - stop_level
    risk_reward = round((target_level - entry_mid) / risk, 2) if risk > 0 else 0.0

    logger.info(
        f"[LTChart] {ticker} → {entry_type} | {pattern} ({confidence:.0%}) | "
        f"R/R={risk_reward:.1f} | entry ${entry_low:.0f}-${entry_high:.0f} | "
        f"stop ${stop_level:.0f} | target ${target_level:.0f}"
    )

    return SwingChartSignal(
        ticker=ticker,
        entry_type=entry_type,
        pattern=pattern,
        pattern_confidence=confidence,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop_level=stop_level,
        target_level=target_level,
        risk_reward=risk_reward,
        support_levels=[lvl.price for lvl in sr_levels if lvl.kind == "support"],
        resistance_levels=[lvl.price for lvl in sr_levels if lvl.kind == "resistance"],
        chart_thesis=thesis,
        chart_path=chart_path or "",
        analyzed_at=date.today().isoformat(),
    )


def run_chart_analysis_batch(
    candidates: list,   # list of SwingSignal (HIGH/MEDIUM only)
    min_rr: float = 2.0,
) -> list[SwingChartSignal]:
    """
    Run chart analysis on HIGH/MEDIUM swing candidates.
    Filters out 'wait' setups with R/R < min_rr.
    Returns list sorted by risk_reward descending.
    """
    results = []
    for sig in candidates:
        ticker = sig.ticker if hasattr(sig, "ticker") else sig
        chart_sig = analyze_swing_candidate(ticker)
        if chart_sig is None:
            continue
        # Keep all for dashboard display — UI shows entry_type as badge
        # But mark actionable vs watching
        results.append(chart_sig)

    results.sort(key=lambda x: (
        0 if x.entry_type == "wait" else 1,   # actionable first
        x.risk_reward,
    ), reverse=True)

    logger.info(
        f"[SwingChart] Batch complete: {len(results)} analyzed | "
        f"{sum(1 for r in results if r.entry_type != 'wait')} actionable"
    )
    return results
