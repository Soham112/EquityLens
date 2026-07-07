"""
Data layer: price/volume, fundamentals, insider trades.
MVP uses yfinance (free). Upgrade path to IEX/FactSet is via settings.
"""
import datetime
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


@dataclass
class PriceData:
    ticker: str
    current_price: float
    price_50d_ma: float
    price_200d_ma: float
    atr_20d: float          # Average True Range, 20-day
    rsi_14: float
    volume_avg_20d: float
    market_cap: float
    last_updated: datetime.date
    volume_avg_90d: Optional[float] = None      # 90-day avg volume for accumulation signal
    short_float_pct: Optional[float] = None     # short interest as % of float
    # Technical analysis extras
    macd_histogram: Optional[float] = None   # positive = bullish momentum
    macd_cross_bullish: Optional[bool] = None  # MACD line crossed above signal in last 3 days
    week_52_high_pct: Optional[float] = None   # 0 = at high, -0.10 = 10% below high
    rs_vs_spy: Optional[float] = None          # 3-month return minus SPY 3-month return
    return_3m: Optional[float] = None          # stock's own 3-month return — for RS vs sector ETF
    stage: Optional[str] = None                # Weinstein stage: "1" | "2" | "3" | "4"
    atr_compression: Optional[float] = None    # current ATR / 3-month avg ATR; <0.7 = coiling


@dataclass
class FundamentalsData:
    ticker: str
    revenue_growth_yoy: Optional[float]     # e.g. 0.18 = 18%
    fcf: Optional[float]                     # Free cash flow, positive = good
    gross_margin: Optional[float]
    debt_to_equity: Optional[float]
    pe_ratio: Optional[float]
    peg_ratio: Optional[float]
    ev_to_fcf: Optional[float]
    last_earnings_date: Optional[datetime.date]
    last_updated: datetime.date
    # Historical depth fields
    earnings_beat_rate: Optional[float] = None      # 0-1: % of last 4 quarters that beat EPS estimates
    earnings_surprise_avg: Optional[float] = None   # avg % surprise (positive = beat)
    revenue_growth_trend: Optional[str] = None      # "ACCELERATING" | "DECELERATING" | "STABLE"
    quarterly_rev_growth: Optional[list] = None     # last 4 quarters of QoQ revenue growth rates


@dataclass
class InsiderData:
    ticker: str
    ceo_cfo_selling: bool       # True if unusual CEO/CFO selling detected
    ceo_cfo_buying: bool
    net_insider_signal: str     # "BULLISH" | "BEARISH" | "NEUTRAL"
    last_updated: datetime.date


def _calc_atr(hist: pd.DataFrame, period: int = 20) -> float:
    high = hist["High"]
    low = hist["Low"]
    close = hist["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def _calc_macd(close: pd.Series) -> tuple[float, bool]:
    """Returns (histogram_value, cross_bullish_in_last_3_days)."""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    hist = macd_line - signal_line
    # Bullish cross: histogram flipped from negative to positive in last 3 bars
    recent = hist.iloc[-3:]
    cross_bullish = bool(recent.iloc[-1] > 0 and recent.iloc[0] <= 0)
    return float(hist.iloc[-1]), cross_bullish


# SPY 3-month return, cached per calendar day to avoid one fetch per ticker
_spy_cache: dict[datetime.date, float] = {}


def _get_spy_3m_return() -> Optional[float]:
    today = datetime.date.today()
    if today in _spy_cache:
        return _spy_cache[today]
    try:
        spy_hist = yf.Ticker("SPY").history(period="3mo")
        if spy_hist.empty or len(spy_hist) < 2:
            return None
        c = spy_hist["Close"]
        ret = float((c.iloc[-1] - c.iloc[0]) / c.iloc[0])
        _spy_cache[today] = ret
        return ret
    except Exception:
        return None


def _calc_stage(close: pd.Series, ma50: float, ma200: float) -> str:
    """
    Weinstein stage analysis:
      Stage 1 — Basing: price near MA200, MA200 flat
      Stage 2 — Uptrend: price > MA50 > MA200, MA200 rising  ← only buy here
      Stage 3 — Topping: price volatile near highs, MA50 turning down
      Stage 4 — Downtrend: price < MA50 < MA200
    """
    current = float(close.iloc[-1])
    ma200_3m_ago = float(close.rolling(200).mean().iloc[-63]) if len(close) >= 263 else ma200
    ma200_rising = ma200 > ma200_3m_ago

    if current > ma50 and ma50 > ma200 and ma200_rising:
        return "2"   # Classic Stage 2 uptrend — ideal entry
    elif current > ma50 and ma50 > ma200:
        return "2"   # Stage 2 without confirmed MA200 slope yet
    elif current < ma50 and ma50 < ma200:
        return "4"   # Downtrend — avoid
    elif current > ma200 and not (current > ma50):
        return "3"   # Price dipped below MA50 but still above MA200 — topping
    else:
        return "1"   # Basing near lows


def _calc_atr_compression(hist: pd.DataFrame) -> Optional[float]:
    """ATR compression ratio: current 10-day ATR / 3-month avg ATR. <0.7 = tight coil."""
    try:
        high, low, close = hist["High"], hist["Low"], hist["Close"]
        prev_close = close.shift(1)
        tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr_10 = float(tr.rolling(10).mean().iloc[-1])
        atr_63 = float(tr.rolling(63).mean().iloc[-1]) if len(tr) >= 63 else None
        if atr_63 and atr_63 > 0:
            return round(atr_10 / atr_63, 3)
    except Exception:
        pass
    return None


def fetch_price_data(ticker: str) -> Optional[PriceData]:
    try:
        stock = yf.Ticker(ticker)
        # 1y gives us 252 days — needed for accurate 52-week high and MA200
        hist = stock.history(period="1y")
        if hist.empty:
            logger.warning(f"No price history for {ticker}")
            return None

        info = stock.info
        close = hist["Close"]
        atr = _calc_atr(hist)
        rsi = _calc_rsi(close)
        macd_hist, macd_cross = _calc_macd(close)

        # 52-week high proximity (0 = at the high, -0.15 = 15% below)
        high_52w = float(close.max())
        current = float(close.iloc[-1])
        week_52_high_pct = (current - high_52w) / high_52w if high_52w else None

        # Relative strength vs SPY over 3 months
        spy_ret = _get_spy_3m_return()
        stock_3m = None
        if len(close) >= 63:
            stock_3m = float((close.iloc[-1] - close.iloc[-63]) / close.iloc[-63])
        rs_vs_spy = (stock_3m - spy_ret) if (spy_ret is not None and stock_3m is not None) else None

        ma50 = float(close.rolling(50).mean().iloc[-1])
        ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else float(close.mean())

        vol_90d = float(hist["Volume"].rolling(90).mean().iloc[-1]) if len(hist) >= 90 else None
        short_float = info.get("shortPercentOfFloat")
        if short_float and short_float > 1:   # yfinance sometimes returns 0-100 scale
            short_float = short_float / 100.0

        return PriceData(
            ticker=ticker,
            current_price=current,
            price_50d_ma=ma50,
            price_200d_ma=ma200,
            atr_20d=atr,
            rsi_14=rsi,
            volume_avg_20d=float(hist["Volume"].rolling(20).mean().iloc[-1]),
            volume_avg_90d=vol_90d,
            short_float_pct=round(float(short_float), 4) if short_float else None,
            market_cap=float(info.get("marketCap", 0)),
            last_updated=datetime.date.today(),
            macd_histogram=round(macd_hist, 4),
            macd_cross_bullish=macd_cross,
            week_52_high_pct=round(week_52_high_pct, 4) if week_52_high_pct is not None else None,
            rs_vs_spy=round(rs_vs_spy, 4) if rs_vs_spy is not None else None,
            return_3m=round(stock_3m, 4) if stock_3m is not None else None,
            stage=_calc_stage(close, ma50, ma200),
            atr_compression=_calc_atr_compression(hist),
        )
    except Exception as e:
        logger.error(f"fetch_price_data({ticker}): {e}")
        return None


def fetch_fundamentals(ticker: str) -> Optional[FundamentalsData]:
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        # Revenue growth: compare trailing vs previous annual
        revenue = info.get("totalRevenue")
        revenue_growth = None
        if revenue:
            # yfinance doesn't give YoY directly; use revenueGrowth if available
            revenue_growth = info.get("revenueGrowth")  # e.g. 0.18

        # Earnings date
        earnings_date = None
        try:
            cal = stock.calendar
            if cal is not None and not cal.empty:
                raw = cal.iloc[0].get("Earnings Date")
                if raw:
                    earnings_date = pd.Timestamp(raw).date()
        except Exception:
            pass

        fcf = info.get("freeCashflow")
        gross_margin = info.get("grossMargins")
        de_ratio = info.get("debtToEquity")
        if de_ratio:
            de_ratio = de_ratio / 100  # yfinance gives it as percentage

        # ── Earnings beat/miss history (last 4 quarters) ──
        earnings_beat_rate = None
        earnings_surprise_avg = None
        try:
            eh = stock.earnings_history
            if eh is not None and not eh.empty and len(eh) >= 2:
                eh = eh.dropna(subset=["epsEstimate", "epsActual"]).tail(4)
                if len(eh) >= 2:
                    beats = (eh["epsActual"] > eh["epsEstimate"]).sum()
                    earnings_beat_rate = round(float(beats) / len(eh), 2)
                    surprises = ((eh["epsActual"] - eh["epsEstimate"]) / eh["epsEstimate"].abs().replace(0, float("nan"))).dropna()
                    if not surprises.empty:
                        earnings_surprise_avg = round(float(surprises.mean()), 3)
        except Exception:
            pass

        # ── Revenue trend: accelerating / decelerating / stable ──
        revenue_growth_trend = None
        quarterly_rev_growth = None
        try:
            qf = stock.quarterly_financials
            if qf is not None and not qf.empty and "Total Revenue" in qf.index:
                rev = qf.loc["Total Revenue"].dropna().sort_index()
                if len(rev) >= 5:
                    # QoQ growth for last 4 quarters
                    qoq = [(float(rev.iloc[i]) - float(rev.iloc[i-1])) / abs(float(rev.iloc[i-1]))
                           for i in range(1, len(rev))]
                    quarterly_rev_growth = [round(g, 3) for g in qoq[-4:]]
                    # Trend: compare avg of last 2 vs prior 2
                    if len(quarterly_rev_growth) >= 4:
                        recent = sum(quarterly_rev_growth[-2:]) / 2
                        prior  = sum(quarterly_rev_growth[:2]) / 2
                        if recent > prior + 0.03:
                            revenue_growth_trend = "ACCELERATING"
                        elif recent < prior - 0.03:
                            revenue_growth_trend = "DECELERATING"
                        else:
                            revenue_growth_trend = "STABLE"
        except Exception:
            pass

        return FundamentalsData(
            ticker=ticker,
            revenue_growth_yoy=revenue_growth,
            fcf=float(fcf) if fcf else None,
            gross_margin=float(gross_margin) if gross_margin else None,
            debt_to_equity=float(de_ratio) if de_ratio else None,
            pe_ratio=info.get("trailingPE"),
            peg_ratio=info.get("pegRatio"),
            ev_to_fcf=None,
            last_earnings_date=earnings_date,
            last_updated=datetime.date.today(),
            earnings_beat_rate=earnings_beat_rate,
            earnings_surprise_avg=earnings_surprise_avg,
            revenue_growth_trend=revenue_growth_trend,
            quarterly_rev_growth=quarterly_rev_growth,
        )
    except Exception as e:
        logger.error(f"fetch_fundamentals({ticker}): {e}")
        return None


def fetch_insider_data(ticker: str) -> InsiderData:
    """
    SEC Form 4 insider data. MVP: basic heuristic from yfinance.
    Upgrade: Insiderscore or SEC EDGAR direct parsing.
    """
    try:
        stock = yf.Ticker(ticker)
        trades = stock.insider_transactions
        if trades is None or trades.empty:
            return InsiderData(
                ticker=ticker,
                ceo_cfo_selling=False,
                ceo_cfo_buying=False,
                net_insider_signal="NEUTRAL",
                last_updated=datetime.date.today(),
            )

        cutoff = datetime.date.today() - datetime.timedelta(days=90)
        recent = trades[pd.to_datetime(trades.get("Start Date", trades.index)).dt.date >= cutoff]

        exec_titles = ["CEO", "CFO", "Chief Executive", "Chief Financial"]
        exec_trades = recent[
            recent.get("Relationship", "").str.upper().str.contains("|".join(exec_titles), na=False)
        ] if "Relationship" in recent.columns else recent

        selling = exec_trades[exec_trades.get("Transaction", "").str.contains("Sale", na=False, case=False)] if "Transaction" in exec_trades.columns else pd.DataFrame()
        buying = exec_trades[exec_trades.get("Transaction", "").str.contains("Buy|Purchase", na=False, case=False)] if "Transaction" in exec_trades.columns else pd.DataFrame()

        ceo_cfo_selling = len(selling) >= 2
        ceo_cfo_buying = len(buying) >= 1

        if ceo_cfo_buying and not ceo_cfo_selling:
            signal = "BULLISH"
        elif ceo_cfo_selling and not ceo_cfo_buying:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"

        return InsiderData(
            ticker=ticker,
            ceo_cfo_selling=ceo_cfo_selling,
            ceo_cfo_buying=ceo_cfo_buying,
            net_insider_signal=signal,
            last_updated=datetime.date.today(),
        )
    except Exception as e:
        logger.error(f"fetch_insider_data({ticker}): {e}")
        return InsiderData(
            ticker=ticker,
            ceo_cfo_selling=False,
            ceo_cfo_buying=False,
            net_insider_signal="NEUTRAL",
            last_updated=datetime.date.today(),
        )


def fetch_sector_return(sector_etf: str, days: int = 60) -> Optional[float]:
    """Returns rolling return for a sector ETF over `days` calendar days."""
    try:
        stock = yf.Ticker(sector_etf)
        hist = stock.history(period=f"{days + 30}d")
        if len(hist) < 2:
            return None
        hist = hist.tail(days)
        start = float(hist["Close"].iloc[0])
        end = float(hist["Close"].iloc[-1])
        return (end - start) / start
    except Exception as e:
        logger.error(f"fetch_sector_return({sector_etf}, {days}d): {e}")
        return None


# Sector ETF map
SECTOR_ETF_MAP = {
    "semiconductors": "SMH",
    "technology": "XLK",
    "healthcare": "XLV",
    "energy": "XLE",
    "financials": "XLF",
    "consumer_discretionary": "XLY",
    "industrials": "XLI",
    "ai_infrastructure": "SOXX",
    "data_centers": "SRVR",
    "market": "SPY",
}
