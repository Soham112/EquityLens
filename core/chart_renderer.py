"""
Chart Renderer — generates a clean candlestick chart as a PNG bytes object.
Ported from Stock_prediction_GPT-4o/Stock_analysis/candles_charts.py and tightened.

Output: raw PNG bytes (no disk write required) — passed directly to vision API.
Optional: save_path to persist for debugging.
"""
import io
import logging
from typing import Optional

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — no display needed
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
import yfinance as yf
from mplfinance.original_flavor import candlestick_ohlc

logger = logging.getLogger(__name__)

# Chart look — matches the clean style from the original project
_STYLE = {
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.grid": True,
    "grid.alpha": 0.3,
}


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def render_chart(
    ticker: str,
    period: str = "5y",
    interval: str = "1wk",
    save_path: Optional[str] = None,
) -> Optional[bytes]:
    """
    Fetch price history and render a two-panel chart:
      Top: candlesticks + MA50 + MA200 + volume (twin axis) + last-close line
      Bottom: RSI(14) with 70/30 bands

    Returns PNG as raw bytes, or None on failure.
    period/interval: "2y"/"1wk" gives ~100 weekly candles — clean signal, not noise.
    """
    try:
        raw = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    except Exception as e:
        logger.error(f"[ChartRenderer] yfinance download failed for {ticker}: {e}")
        return None

    if raw is None or len(raw) < 60:
        logger.warning(f"[ChartRenderer] Insufficient data for {ticker} ({len(raw) if raw is not None else 0} bars)")
        return None

    # Flatten MultiIndex columns if present (yfinance >=0.2.38)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy().dropna()

    df["MA50"]  = df["Close"].rolling(50).mean()
    df["MA200"] = df["Close"].rolling(200).mean()
    df["RSI"]   = _calc_rsi(df["Close"])

    # Golden / death cross events — mark on chart
    df["cross"] = (df["MA50"] > df["MA200"]).astype(int)
    df["cross_signal"] = df["cross"].diff()  # +1 = golden cross, -1 = death cross

    df["DateNum"] = mdates.date2num(df.index.to_pydatetime())
    ohlc = df[["DateNum", "Open", "High", "Low", "Close"]].dropna().values

    last_close = float(df["Close"].iloc[-1])
    ma50_last  = float(df["MA50"].iloc[-1])  if df["MA50"].notna().any()  else None
    ma200_last = float(df["MA200"].iloc[-1]) if df["MA200"].notna().any() else None

    with plt.rc_context(_STYLE):
        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(14, 10),
            gridspec_kw={"height_ratios": [3, 1]},
        )

        # ── Top panel ──────────────────────────────────────────────────
        candlestick_ohlc(ax1, ohlc, width=3.5, colorup="#26a69a", colordown="#ef5350", alpha=0.85)

        ax1.plot(df["DateNum"], df["MA50"],  color="#f39c12", linewidth=1.6, label=f"MA50  {ma50_last:.0f}"  if ma50_last  else "MA50")
        ax1.plot(df["DateNum"], df["MA200"], color="#e74c3c", linewidth=1.6, label=f"MA200 {ma200_last:.0f}" if ma200_last else "MA200")

        # Mark golden / death crosses
        for idx, row in df[df["cross_signal"] != 0].iterrows():
            is_golden = row["cross_signal"] == 1
            ax1.axvline(
                mdates.date2num(idx.to_pydatetime()),
                color="#27ae60" if is_golden else "#c0392b",
                linewidth=1.2, linestyle="--", alpha=0.7,
            )
            ax1.annotate(
                "Golden ✕" if is_golden else "Death ✕",
                xy=(mdates.date2num(idx.to_pydatetime()), df.loc[idx, "Close"]),
                fontsize=7.5,
                color="#27ae60" if is_golden else "#c0392b",
                xytext=(5, 10), textcoords="offset points",
            )

        # Last close horizontal line
        ax1.axhline(last_close, color="#8e44ad", linestyle="--", linewidth=1, alpha=0.8)
        ax1.text(
            df["DateNum"].iloc[-1], last_close,
            f"  {last_close:.2f}", color="#8e44ad", fontsize=9, va="bottom",
        )

        # Volume on twin axis
        ax1v = ax1.twinx()
        ax1v.bar(df["DateNum"], df["Volume"], width=3.5, color="#90caf9", alpha=0.3)
        ax1v.set_ylabel("Volume", fontsize=8, color="#5d6d7e")
        ax1v.tick_params(axis="y", labelsize=7)
        ax1v.set_ylim(0, df["Volume"].max() * 4)  # push volume to bottom quarter

        ax1.set_title(f"{ticker} — Weekly Price Chart", fontsize=13, fontweight="bold")
        ax1.set_ylabel("Price")
        ax1.legend(loc="upper left", fontsize=8)
        ax1.xaxis_date()
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%b"))
        ax1.tick_params(axis="x", rotation=30, labelsize=8)

        # ── Bottom panel: RSI ───────────────────────────────────────────
        ax2.plot(df.index, df["RSI"], color="#e91e8c", linewidth=1.3, label="RSI(14)")
        ax2.axhline(70, color="#e74c3c", linestyle="--", linewidth=0.9, alpha=0.8)
        ax2.axhline(30, color="#27ae60", linestyle="--", linewidth=0.9, alpha=0.8)
        ax2.fill_between(df.index, df["RSI"], 70, where=(df["RSI"] >= 70), alpha=0.15, color="#e74c3c")
        ax2.fill_between(df.index, df["RSI"], 30, where=(df["RSI"] <= 30), alpha=0.15, color="#27ae60")
        ax2.set_ylim(0, 100)
        ax2.set_ylabel("RSI")
        ax2.set_title(f"{ticker} RSI(14)", fontsize=10)
        ax2.legend(loc="upper left", fontsize=8)
        ax2.tick_params(axis="x", rotation=30, labelsize=8)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        png_bytes = buf.read()

    if save_path:
        with open(save_path, "wb") as f:
            f.write(png_bytes)
        logger.info(f"[ChartRenderer] Saved chart to {save_path}")

    logger.info(f"[ChartRenderer] Rendered {ticker} chart ({len(png_bytes)//1024}KB)")
    return png_bytes
