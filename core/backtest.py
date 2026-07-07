"""
Backtesting Framework [GAP 9]

Two modes:
  1. Signal-replay   — reads stored scan results (data/scan_*.json or data/daily_scan_*.json),
                       fetches forward prices from yfinance, measures return / stop outcomes.
  2. Historical-scan — re-runs the scoring pipeline on a past date using yfinance
                       OHLCV history (no BigData dependency; sentiment set to neutral).

Usage:
  from core.backtest import run_signal_replay, BacktestConfig
  report = run_signal_replay(config=BacktestConfig(hold_days=[5, 10, 20]))
  print(report.summary())
"""
import datetime
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    hold_days: list[int] = field(default_factory=lambda: [5, 10, 20, 60])
    min_conviction: float = 0.0       # filter: only score conviction >= this
    signals_to_include: list[str] = field(default_factory=lambda: ["BUY", "WATCHLIST", "AVOID"])
    # For stop-hit analysis — use tier3 (hard stop) as the stop price
    use_tier3_stop: bool = True
    # Look back this many calendar days when searching for scan files
    scan_lookback_days: int = 90


# ── Outcome per signal ────────────────────────────────────────────────────────

@dataclass
class SignalOutcome:
    ticker: str
    signal_date: str          # date the scan ran
    signal: str               # BUY | WATCHLIST | AVOID
    conviction: float
    entry_price: float        # close on signal_date + 1 trading day
    stop_price: Optional[float]  # tier3 stop from the scan result

    # Forward returns (None if insufficient history)
    returns: dict[int, Optional[float]]   # {hold_days: return_pct}
    stop_hit: Optional[bool]              # did price hit stop_price within max(hold_days)?
    stop_hit_day: Optional[int]           # which day the stop was hit

    max_drawdown: Optional[float]         # worst intraday draw from entry within hold window
    max_gain: Optional[float]             # best close from entry within hold window


# ── Report ───────────────────────────────────────────────────────────────────

@dataclass
class BacktestReport:
    config: BacktestConfig
    generated_at: str
    total_signals: int
    outcomes: list[SignalOutcome]

    def _filter(self, signal: str) -> list[SignalOutcome]:
        return [o for o in self.outcomes if o.signal == signal]

    def _hit_rate(self, outcomes: list[SignalOutcome], hold: int) -> Optional[float]:
        valid = [o for o in outcomes if o.returns.get(hold) is not None]
        if not valid:
            return None
        return sum(1 for o in valid if (o.returns[hold] or 0) > 0) / len(valid)

    def _avg_return(self, outcomes: list[SignalOutcome], hold: int) -> Optional[float]:
        vals = [o.returns[hold] for o in outcomes if o.returns.get(hold) is not None]
        return sum(vals) / len(vals) if vals else None

    def _stop_hit_rate(self, outcomes: list[SignalOutcome]) -> Optional[float]:
        valid = [o for o in outcomes if o.stop_hit is not None]
        if not valid:
            return None
        return sum(1 for o in valid if o.stop_hit) / len(valid)

    def _payoff_ratio(self, outcomes: list[SignalOutcome], hold: int) -> Optional[float]:
        rets = [o.returns[hold] for o in outcomes if o.returns.get(hold) is not None]
        winners = [r for r in rets if r > 0]
        losers = [abs(r) for r in rets if r < 0]
        if not winners or not losers:
            return None
        return (sum(winners) / len(winners)) / (sum(losers) / len(losers))

    def summary(self) -> str:
        lines = [
            f"EquityLens Backtest — {self.generated_at}",
            f"Signals analyzed: {self.total_signals}",
            "",
        ]
        for sig in ["BUY", "WATCHLIST", "AVOID"]:
            group = self._filter(sig)
            if not group:
                continue
            lines.append(f"── {sig} ({len(group)} signals) ──")
            for h in self.config.hold_days:
                hr = self._hit_rate(group, h)
                ar = self._avg_return(group, h)
                pr = self._payoff_ratio(group, h)
                hr_str = f"{hr:.0%}" if hr is not None else "n/a"
                ar_str = f"{ar:+.1%}" if ar is not None else "n/a"
                pr_str = f"{pr:.2f}x" if pr is not None else "n/a"
                lines.append(f"  {h:>3}d: hit={hr_str}  avg={ar_str}  payoff={pr_str}")
            shr = self._stop_hit_rate(group)
            if shr is not None:
                lines.append(f"  Stop-hit rate: {shr:.0%}")
            lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        def _outcome_dict(o: SignalOutcome) -> dict:
            return {
                "ticker": o.ticker,
                "signal_date": o.signal_date,
                "signal": o.signal,
                "conviction": o.conviction,
                "entry_price": o.entry_price,
                "stop_price": o.stop_price,
                "returns": {str(k): v for k, v in o.returns.items()},
                "stop_hit": o.stop_hit,
                "stop_hit_day": o.stop_hit_day,
                "max_drawdown": o.max_drawdown,
                "max_gain": o.max_gain,
            }
        return {
            "generated_at": self.generated_at,
            "total_signals": self.total_signals,
            "outcomes": [_outcome_dict(o) for o in self.outcomes],
        }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_scan_files(lookback_days: int) -> list[tuple[str, list[dict]]]:
    """
    Returns [(date_str, [result_dicts])] from scan_*.json and daily_scan_*.json,
    newest first, within lookback_days.
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
    scans: list[tuple[str, list[dict]]] = []

    if not DATA_DIR.exists():
        return scans

    for f in sorted(DATA_DIR.glob("*.json"), reverse=True):
        name = f.stem
        # Match scan_YYYY-MM-DD or daily_scan_YYYY-MM-DD
        for prefix in ("scan_", "daily_scan_"):
            if name.startswith(prefix):
                date_str = name[len(prefix):]
                if len(date_str) == 10 and date_str >= cutoff:
                    try:
                        with open(f) as fp:
                            data = json.load(fp)
                        results = data.get("results", [])
                        if results:
                            scans.append((date_str, results))
                    except Exception:
                        pass
                break

    return scans


def _fetch_forward_prices(
    ticker: str,
    from_date: datetime.date,
    days_needed: int,
) -> Optional[list[float]]:
    """
    Returns a list of daily closing prices starting from from_date (inclusive),
    up to days_needed+10 trading days out.
    Returns None if data is unavailable.
    """
    end = from_date + datetime.timedelta(days=days_needed + 30)
    try:
        hist = yf.Ticker(ticker).history(
            start=from_date.isoformat(),
            end=end.isoformat(),
        )
        if hist.empty:
            return None
        return [float(c) for c in hist["Close"].tolist()]
    except Exception as e:
        logger.debug(f"{ticker} forward prices: {e}")
        return None


def _calendar_to_trading_days(prices: list[float], calendar_days: int) -> Optional[float]:
    """
    Rough mapping: 1 calendar day ≈ 0.71 trading days.
    Returns price at the nearest trading-day index.
    """
    trading_idx = min(round(calendar_days * 0.71), len(prices) - 1)
    if trading_idx < 0 or trading_idx >= len(prices):
        return None
    return prices[trading_idx]


def _analyze_forward(
    entry_price: float,
    stop_price: Optional[float],
    prices: list[float],
    hold_days: list[int],
    max_hold: int,
) -> tuple[dict[int, Optional[float]], Optional[bool], Optional[int], Optional[float], Optional[float]]:
    """
    Given prices[0] = entry_price (day after signal), compute:
      returns dict, stop_hit, stop_hit_day, max_drawdown, max_gain
    """
    returns: dict[int, Optional[float]] = {}
    for h in hold_days:
        p = _calendar_to_trading_days(prices, h)
        returns[h] = round((p - entry_price) / entry_price, 4) if p is not None else None

    # Stop analysis
    stop_hit = None
    stop_hit_day = None
    max_dd = None
    max_gain_val = None

    if prices:
        window = prices[:round(max_hold * 0.71) + 5]
        lows = window  # using close as proxy (no intraday data from yfinance default)
        max_gain_val = round((max(window) - entry_price) / entry_price, 4) if window else None
        max_dd = round((min(window) - entry_price) / entry_price, 4) if window else None

        if stop_price is not None:
            stop_hit = False
            for i, p in enumerate(window):
                if p <= stop_price:
                    stop_hit = True
                    stop_hit_day = round(i / 0.71)
                    break

    return returns, stop_hit, stop_hit_day, max_dd, max_gain_val


# ── Public API ────────────────────────────────────────────────────────────────

def run_signal_replay(
    config: Optional[BacktestConfig] = None,
    save_report: bool = True,
) -> BacktestReport:
    """
    Load historical scan files, fetch forward prices, compute outcomes.
    Best run after several days of daily scans have accumulated.
    """
    config = config or BacktestConfig()
    max_hold = max(config.hold_days)
    outcomes: list[SignalOutcome] = []

    scans = _load_scan_files(config.scan_lookback_days)
    logger.info(f"Backtest: found {len(scans)} scan files over last {config.scan_lookback_days}d")

    for date_str, results in scans:
        try:
            signal_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue

        # Need at least max_hold days of history after the signal date
        days_since = (datetime.date.today() - signal_date).days
        if days_since < 5:
            logger.debug(f"Skipping {date_str} — too recent for forward analysis")
            continue

        for r in results:
            sig = r.get("signal", "")
            if sig not in config.signals_to_include:
                continue
            conv = float(r.get("conviction", 0))
            if conv < config.min_conviction:
                continue

            ticker = r.get("ticker", "")
            stop_price = r.get("stop_tier3") if config.use_tier3_stop else None

            # Fetch forward prices (start from day after signal)
            from_date = signal_date + datetime.timedelta(days=1)
            prices = _fetch_forward_prices(ticker, from_date, max_hold)
            if not prices:
                logger.debug(f"{ticker} {date_str}: no forward price data")
                continue

            entry_price = prices[0]

            returns, stop_hit, stop_hit_day, max_dd, max_gain = _analyze_forward(
                entry_price, stop_price, prices, config.hold_days, max_hold
            )

            outcomes.append(SignalOutcome(
                ticker=ticker,
                signal_date=date_str,
                signal=sig,
                conviction=conv,
                entry_price=entry_price,
                stop_price=stop_price,
                returns=returns,
                stop_hit=stop_hit,
                stop_hit_day=stop_hit_day,
                max_drawdown=max_dd,
                max_gain=max_gain,
            ))

    report = BacktestReport(
        config=config,
        generated_at=datetime.datetime.now().isoformat(),
        total_signals=len(outcomes),
        outcomes=outcomes,
    )

    if save_report:
        _save_backtest_report(report)

    logger.info(f"Backtest complete: {len(outcomes)} signal outcomes")
    return report


def run_historical_scan(
    tickers: list[tuple[str, str]],
    as_of_date: datetime.date,
    config: Optional[BacktestConfig] = None,
) -> BacktestReport:
    """
    Run the scoring pipeline on historical yfinance data for as_of_date.
    BigData layer is skipped (sentiment neutral). Good for cold-start validation
    when no stored scan files exist yet.

    as_of_date: the date to simulate. Needs at least max(hold_days) trading days
                of future history, so use a date at least 3 months in the past.
    """
    config = config or BacktestConfig()
    max_hold = max(config.hold_days)
    outcomes: list[SignalOutcome] = []

    date_str = as_of_date.isoformat()
    logger.info(f"Historical scan: {len(tickers)} tickers as of {date_str}")

    for ticker, sector in tickers:
        try:
            # Fetch enough history for indicators (200d MA needs ~200 days before as_of)
            hist_start = as_of_date - datetime.timedelta(days=280)
            hist_end = as_of_date + datetime.timedelta(days=max_hold + 30)

            t = yf.Ticker(ticker)
            hist = t.history(start=hist_start.isoformat(), end=hist_end.isoformat())
            if hist.empty or len(hist) < 50:
                logger.debug(f"{ticker}: insufficient history")
                continue

            # Slice to as_of_date
            as_of_ts = as_of_date.isoformat()
            past = hist[hist.index.date <= as_of_date]
            future = hist[hist.index.date > as_of_date]

            if past.empty or future.empty:
                continue

            closes = past["Close"].values
            current_price = float(closes[-1])
            entry_price = float(future["Close"].iloc[0])  # next day open proxy

            # Simple scoring without BigData (fundamentals from yfinance only)
            conviction, signal = _score_historical(ticker, past, closes)

            stop_price = None
            if config.use_tier3_stop:
                atr = _calc_atr(past, 20)
                stop_price = round(current_price - 4.5 * atr, 2) if atr else None

            forward_prices = [float(c) for c in future["Close"].tolist()]
            returns, stop_hit, stop_hit_day, max_dd, max_gain = _analyze_forward(
                entry_price, stop_price, forward_prices, config.hold_days, max_hold
            )

            outcomes.append(SignalOutcome(
                ticker=ticker,
                signal_date=date_str,
                signal=signal,
                conviction=conviction,
                entry_price=entry_price,
                stop_price=stop_price,
                returns=returns,
                stop_hit=stop_hit,
                stop_hit_day=stop_hit_day,
                max_drawdown=max_dd,
                max_gain=max_gain,
            ))

        except Exception as e:
            logger.warning(f"{ticker} historical scan error: {e}")

    report = BacktestReport(
        config=config,
        generated_at=datetime.datetime.now().isoformat(),
        total_signals=len(outcomes),
        outcomes=outcomes,
    )
    _save_backtest_report(report, prefix=f"backtest_historical_{date_str}")
    return report


# ── Lightweight historical scorer (no BigData) ────────────────────────────────

def _calc_atr(hist, period: int = 20) -> Optional[float]:
    try:
        import numpy as np
        closes = hist["Close"].values[-period:]
        highs = hist["High"].values[-period:]
        lows = hist["Low"].values[-period:]
        trs = [max(h - l, abs(h - c), abs(l - c))
               for h, l, c in zip(highs[1:], lows[1:], closes[:-1])]
        return float(np.mean(trs)) if trs else None
    except Exception:
        return None


def _score_historical(
    ticker: str,
    hist,
    closes,
) -> tuple[float, str]:
    """
    Lightweight technicals-only score for historical simulation.
    Returns (conviction_float, signal_str).
    """
    try:
        import numpy as np
        if len(closes) < 50:
            return 0.0, "AVOID"

        price = float(closes[-1])
        ma50 = float(np.mean(closes[-50:]))
        ma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else ma50

        # RSI-14
        deltas = np.diff(closes[-15:])
        gains = deltas[deltas > 0]
        losses = -deltas[deltas < 0]
        avg_gain = float(np.mean(gains)) if len(gains) else 0.0
        avg_loss = float(np.mean(losses)) if len(losses) else 1e-9
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        # Volume trend (20d avg vs 50d avg)
        vol = hist["Volume"].values
        vol_ratio = float(np.mean(vol[-20:])) / float(np.mean(vol[-50:])) if len(vol) >= 50 else 1.0

        # Score components (0-10 scale)
        tech_score = 0.0
        # Trend
        if price > ma50 > ma200:
            tech_score += 4.0  # clear uptrend
        elif price > ma50:
            tech_score += 2.5
        elif price < ma50 < ma200:
            tech_score += 0.0  # downtrend
        else:
            tech_score += 1.0

        # Momentum (RSI sweet spot 50-70)
        if 50 <= rsi <= 70:
            tech_score += 3.0
        elif 40 <= rsi < 50:
            tech_score += 1.5
        elif rsi > 70:
            tech_score += 2.0  # overbought but trending
        else:
            tech_score += 0.0  # oversold / weak

        # Volume confirmation
        if vol_ratio > 1.2:
            tech_score += 2.0
        elif vol_ratio > 0.9:
            tech_score += 1.0

        # No fundamentals in historical mode → use data_confidence = 6 (neutral)
        conviction = min(round(tech_score, 1), 10.0)

        if conviction >= 8:
            signal = "BUY"
        elif conviction >= 6:
            signal = "WATCHLIST"
        else:
            signal = "AVOID"

        return conviction, signal

    except Exception as e:
        logger.debug(f"{ticker} historical score error: {e}")
        return 0.0, "AVOID"


# ── Persistence ───────────────────────────────────────────────────────────────

def _save_backtest_report(report: BacktestReport, prefix: str = "backtest") -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    path = DATA_DIR / f"{prefix}_{date_str}.json"
    with open(path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)
    logger.info(f"Backtest report saved → {path}")


def load_latest_backtest() -> Optional[dict]:
    if not DATA_DIR.exists():
        return None
    files = sorted(DATA_DIR.glob("backtest_*.json"), reverse=True)
    if not files:
        return None
    with open(files[0]) as f:
        return json.load(f)


# ── GAP 16: Baseline comparison ───────────────────────────────────────────────

@dataclass
class BaselineComparison:
    """
    Compares BUY signal returns against a "do nothing" SPY buy-and-hold baseline
    over the same entry dates and hold periods.
    """
    hold_days: list[int]
    signal_avg_return: dict[int, Optional[float]]   # {hold_days: avg return for BUY signals}
    baseline_avg_return: dict[int, Optional[float]] # {hold_days: avg SPY return same periods}
    alpha: dict[int, Optional[float]]               # signal - baseline
    hit_rate_vs_spy: dict[int, Optional[float]]     # % of BUYs that beat SPY same period
    information_ratio: dict[int, Optional[float]]   # alpha / tracking_error
    n_signals: int
    summary: str

    def to_dict(self) -> dict:
        return {
            "n_signals": self.n_signals,
            "hold_days": self.hold_days,
            "signal_avg_return": {str(k): v for k, v in self.signal_avg_return.items()},
            "baseline_avg_return": {str(k): v for k, v in self.baseline_avg_return.items()},
            "alpha": {str(k): v for k, v in self.alpha.items()},
            "hit_rate_vs_spy": {str(k): v for k, v in self.hit_rate_vs_spy.items()},
            "information_ratio": {str(k): v for k, v in self.information_ratio.items()},
            "summary": self.summary,
        }


def _fetch_spy_returns_on_dates(
    entry_dates: list[datetime.date],
    hold_days: list[int],
) -> dict[str, dict[int, Optional[float]]]:
    """
    For each entry_date, fetch SPY forward return at each hold period.
    Returns {date_str: {hold_days: return_pct}}.
    """
    if not entry_dates:
        return {}

    min_date = min(entry_dates)
    max_hold = max(hold_days)
    end_date = datetime.date.today()

    try:
        spy_hist = yf.Ticker("SPY").history(
            start=(min_date - datetime.timedelta(days=5)).isoformat(),
            end=(end_date + datetime.timedelta(days=1)).isoformat(),
        )
        if spy_hist.empty:
            return {}
        spy_closes = spy_hist["Close"]
        spy_dates = [d.date() for d in spy_hist.index]
    except Exception as e:
        logger.warning(f"SPY fetch error: {e}")
        return {}

    result: dict[str, dict[int, Optional[float]]] = {}

    for entry_date in entry_dates:
        date_str = entry_date.isoformat()
        # Find closest SPY date on or after entry
        start_idx = None
        for i, d in enumerate(spy_dates):
            if d >= entry_date:
                start_idx = i
                break
        if start_idx is None:
            result[date_str] = {h: None for h in hold_days}
            continue

        entry_price = float(spy_closes.iloc[start_idx])
        returns: dict[int, Optional[float]] = {}
        for h in hold_days:
            target_date = entry_date + datetime.timedelta(days=h)
            # Find closest date on or before target
            fwd_idx = None
            for i in range(start_idx, len(spy_dates)):
                if spy_dates[i] >= target_date:
                    fwd_idx = i
                    break
            if fwd_idx is None or fwd_idx == start_idx:
                returns[h] = None
            else:
                fwd_price = float(spy_closes.iloc[fwd_idx])
                returns[h] = round((fwd_price - entry_price) / entry_price, 4)
        result[date_str] = returns

    return result


def compare_to_baseline(report: BacktestReport) -> BaselineComparison:
    """
    GAP 16: Compare BUY signal returns to SPY buy-and-hold over the same periods.
    Returns a BaselineComparison. Attach to BacktestReport for the full picture.
    """
    buy_outcomes = [o for o in report.outcomes if o.signal == "BUY"]
    hold_days = report.config.hold_days

    if not buy_outcomes:
        empty = {h: None for h in hold_days}
        return BaselineComparison(
            hold_days=hold_days,
            signal_avg_return=empty,
            baseline_avg_return=empty,
            alpha=empty,
            hit_rate_vs_spy=empty,
            information_ratio=empty,
            n_signals=0,
            summary="No BUY signals to compare against baseline",
        )

    # Unique entry dates
    entry_dates = list({
        datetime.date.fromisoformat(o.signal_date) + datetime.timedelta(days=1)
        for o in buy_outcomes
    })
    spy_returns_by_date = _fetch_spy_returns_on_dates(entry_dates, hold_days)

    # Per-signal excess returns (signal return - SPY return same date+hold)
    import math
    signal_avg: dict[int, Optional[float]] = {}
    baseline_avg: dict[int, Optional[float]] = {}
    alpha: dict[int, Optional[float]] = {}
    hit_vs_spy: dict[int, Optional[float]] = {}
    info_ratio: dict[int, Optional[float]] = {}

    for h in hold_days:
        pairs: list[tuple[float, float]] = []  # (signal_ret, spy_ret)
        for o in buy_outcomes:
            sig_ret = o.returns.get(h)
            if sig_ret is None:
                continue
            entry_key = (datetime.date.fromisoformat(o.signal_date) + datetime.timedelta(days=1)).isoformat()
            spy_ret = spy_returns_by_date.get(entry_key, {}).get(h)
            if spy_ret is None:
                continue
            pairs.append((sig_ret, spy_ret))

        if not pairs:
            signal_avg[h] = None
            baseline_avg[h] = None
            alpha[h] = None
            hit_vs_spy[h] = None
            info_ratio[h] = None
            continue

        s_rets = [p[0] for p in pairs]
        b_rets = [p[1] for p in pairs]
        excess = [s - b for s, b in pairs]

        s_avg = sum(s_rets) / len(s_rets)
        b_avg = sum(b_rets) / len(b_rets)
        ex_avg = sum(excess) / len(excess)
        signal_avg[h] = round(s_avg, 4)
        baseline_avg[h] = round(b_avg, 4)
        alpha[h] = round(ex_avg, 4)
        hit_vs_spy[h] = round(sum(1 for e in excess if e > 0) / len(excess), 3)

        # Information ratio = alpha / tracking_error
        if len(excess) > 1:
            mean_ex = sum(excess) / len(excess)
            variance = sum((e - mean_ex) ** 2 for e in excess) / (len(excess) - 1)
            te = math.sqrt(variance) if variance > 0 else None
            info_ratio[h] = round(ex_avg / te, 2) if te else None
        else:
            info_ratio[h] = None

    # Summary line
    lines = [f"BUY signals vs SPY baseline ({len(buy_outcomes)} signals):"]
    for h in hold_days:
        a = alpha.get(h)
        ir = info_ratio.get(h)
        hvs = hit_vs_spy.get(h)
        if a is None:
            lines.append(f"  {h:>3}d: insufficient data")
        else:
            ir_str = f"  IR={ir:.2f}" if ir is not None else ""
            lines.append(
                f"  {h:>3}d: alpha={a:+.1%}  beat_spy={hvs:.0%}{ir_str}"
            )

    comparison = BaselineComparison(
        hold_days=hold_days,
        signal_avg_return=signal_avg,
        baseline_avg_return=baseline_avg,
        alpha=alpha,
        hit_rate_vs_spy=hit_vs_spy,
        information_ratio=info_ratio,
        n_signals=len(buy_outcomes),
        summary="\n".join(lines),
    )

    # Save alongside the backtest report
    os.makedirs(DATA_DIR, exist_ok=True)
    date_str = datetime.date.today().isoformat()
    path = DATA_DIR / f"baseline_comparison_{date_str}.json"
    with open(path, "w") as f:
        json.dump(comparison.to_dict(), f, indent=2)
    logger.info(f"Baseline comparison saved → {path}")

    return comparison
