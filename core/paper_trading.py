"""
Paper Trading Engine

Simulates trading with virtual cash against real market prices.
No real money involved — purely for validating the system before going live.

Starting capital: configurable (default $500)
Auto-executes: every BUY signal at the model's recommended position size
Auto-applies:  Tier 3 stop losses, profit-taking trims (+50%/+100%/+200%)

Files:
  data/paper_portfolio.json   — current positions + cash
  data/paper_trades.jsonl     — every trade log entry
  data/paper_pnl_history.json — daily portfolio value snapshots
"""
import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

PAPER_PORTFOLIO_FILE = Path("data/paper_portfolio.json")
PAPER_TRADES_FILE = Path("data/paper_trades.jsonl")
PAPER_PNL_FILE = Path("data/paper_pnl_history.json")

# Unified $5,000 paper pool: $3,500 long-term (here) + $1,500 swing (growth_paper_trading)
STARTING_CAPITAL = 3500.0   # virtual dollars


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class PaperPosition:
    ticker: str
    entry_date: str
    entry_price: float
    shares: float           # fractional shares supported
    current_price: float
    stop_tier1: Optional[float]
    stop_tier2: Optional[float]
    stop_tier3: Optional[float]
    conviction: float
    recommended_pct: float
    cost_basis: float       # entry_price * shares
    peak_price: Optional[float] = None   # highest price seen since entry — drives trailing stop
    atr_at_entry: Optional[float] = None  # ATR when position was opened
    sector: str = "unknown"              # microsector from the scan — drives concentration caps
    trims_taken: list[str] = field(default_factory=list)  # e.g. ["TRIM_25"] — each level fires once

    @property
    def market_value(self) -> float:
        return self.current_price * self.shares

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def return_pct(self) -> float:
        return (self.current_price - self.entry_price) / self.entry_price


@dataclass
class PaperPortfolio:
    cash: float
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    start_date: str = ""
    starting_capital: float = STARTING_CAPITAL

    @property
    def invested_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        return self.cash + self.invested_value

    @property
    def total_return_pct(self) -> float:
        return (self.total_value - self.starting_capital) / self.starting_capital


@dataclass
class PaperTrade:
    date: str
    ticker: str
    action: str             # BUY | SELL_STOP | SELL_TRIM | SELL_FULL
    shares: float
    price: float
    value: float            # shares * price
    conviction: float
    reason: str             # e.g. "BUY signal conviction=8.5" | "Tier 3 stop hit" | "+50% trim"
    portfolio_value_after: float


# ── Persistence ───────────────────────────────────────────────────────────────

def load_paper_portfolio() -> PaperPortfolio:
    if not PAPER_PORTFOLIO_FILE.exists():
        return PaperPortfolio(
            cash=STARTING_CAPITAL,
            start_date=datetime.date.today().isoformat(),
            starting_capital=STARTING_CAPITAL,
        )
    try:
        with open(PAPER_PORTFOLIO_FILE) as f:
            data = json.load(f)
        positions = {
            t: PaperPosition(**p)
            for t, p in data.get("positions", {}).items()
        }
        return PaperPortfolio(
            cash=data["cash"],
            positions=positions,
            start_date=data.get("start_date", ""),
            starting_capital=data.get("starting_capital", STARTING_CAPITAL),
        )
    except Exception as e:
        logger.warning(f"paper portfolio load error: {e}")
        return PaperPortfolio(cash=STARTING_CAPITAL, starting_capital=STARTING_CAPITAL)


def save_paper_portfolio(portfolio: PaperPortfolio) -> None:
    os.makedirs("data", exist_ok=True)
    data = {
        "cash": round(portfolio.cash, 4),
        "start_date": portfolio.start_date,
        "starting_capital": portfolio.starting_capital,
        "positions": {t: asdict(p) for t, p in portfolio.positions.items()},
    }
    with open(PAPER_PORTFOLIO_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _log_trade(trade: PaperTrade) -> None:
    os.makedirs("data", exist_ok=True)
    with open(PAPER_TRADES_FILE, "a") as f:
        f.write(json.dumps(asdict(trade)) + "\n")


def load_trade_history() -> list[PaperTrade]:
    if not PAPER_TRADES_FILE.exists():
        return []
    trades = []
    with open(PAPER_TRADES_FILE) as f:
        for line in f:
            try:
                trades.append(PaperTrade(**json.loads(line.strip())))
            except Exception:
                pass
    return trades


# ── Price fetching ────────────────────────────────────────────────────────────

def _fetch_price(ticker: str) -> Optional[float]:
    try:
        t = yf.Ticker(ticker)
        # Try fast_info for live price during market hours
        price = getattr(t.fast_info, "last_price", None)
        if price and price > 0:
            return float(price)
        # Fallback to history
        hist = t.history(period="2d")
        return float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception:
        return None


def _recent_split_ratio(ticker: str, within_days: int = 7) -> Optional[float]:
    """Split ratio if the stock split in the last few days, else None."""
    try:
        s = yf.Ticker(ticker).splits
        if s is None or len(s) == 0:
            return None
        last_date, last_ratio = s.index[-1], float(s.iloc[-1])
        if (datetime.date.today() - last_date.date()).days <= within_days and last_ratio > 0:
            return last_ratio
    except Exception:
        pass
    return None


def _apply_split(pos: PaperPosition, ratio: float) -> None:
    """Adjust a position in place for a split — shares up, prices/stops down."""
    pos.shares = round(pos.shares * ratio, 6)
    pos.entry_price = round(pos.entry_price / ratio, 4)
    for attr in ("stop_tier1", "stop_tier2", "stop_tier3", "peak_price", "atr_at_entry"):
        v = getattr(pos, attr)
        if v:
            setattr(pos, attr, round(v / ratio, 4))


def _fetch_spy_return_since(since_date: str) -> Optional[float]:
    try:
        hist = yf.Ticker("SPY").history(
            start=since_date,
            end=(datetime.date.today() + datetime.timedelta(days=1)).isoformat(),
        )
        if hist.empty or len(hist) < 2:
            return None
        start_price = float(hist["Close"].iloc[0])
        end_price = float(hist["Close"].iloc[-1])
        return (end_price - start_price) / start_price
    except Exception:
        return None


# ── Core actions ──────────────────────────────────────────────────────────────

def execute_buy(
    ticker: str,
    conviction: float,
    recommended_pct: float,
    stop_tier1: Optional[float],
    stop_tier2: Optional[float],
    stop_tier3: Optional[float],
    reason: str = "",
    sector: str = "unknown",
) -> Optional[PaperTrade]:
    """
    Auto-execute a BUY signal. Allocates recommended_pct of current portfolio value.
    Returns None if insufficient cash or already holding.
    """
    portfolio = load_paper_portfolio()

    if ticker in portfolio.positions:
        logger.info(f"Paper: already holding {ticker} — skip buy")
        return None

    price = _fetch_price(ticker)
    if price is None:
        logger.warning(f"Paper: no price for {ticker} — skip")
        return None

    # Position size = recommended_pct of total portfolio value
    dollar_amount = portfolio.total_value * recommended_pct

    # Enforce minimum ($5) and cap at available cash
    dollar_amount = min(dollar_amount, portfolio.cash * 0.95)  # keep 5% cash buffer
    if dollar_amount < 5.0:
        logger.info(f"Paper: insufficient cash for {ticker} (${portfolio.cash:.2f} available)")
        return None

    shares = dollar_amount / price  # fractional shares

    # Fetch ATR at entry for trailing stop calculations
    atr_at_entry = None
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="30d")
        if not hist.empty:
            high, low, close = hist["High"], hist["Low"], hist["Close"]
            tr = pd.concat([(high-low), (high-close.shift(1)).abs(), (low-close.shift(1)).abs()], axis=1).max(axis=1)
            atr_at_entry = round(float(tr.rolling(20).mean().iloc[-1]), 4)
    except Exception:
        pass

    position = PaperPosition(
        ticker=ticker,
        entry_date=datetime.date.today().isoformat(),
        entry_price=price,
        shares=round(shares, 6),
        current_price=price,
        stop_tier1=stop_tier1,
        stop_tier2=stop_tier2,
        stop_tier3=stop_tier3,
        conviction=conviction,
        recommended_pct=recommended_pct,
        cost_basis=round(dollar_amount, 4),
        peak_price=price,
        atr_at_entry=atr_at_entry,
        sector=sector,
    )

    portfolio.positions[ticker] = position
    portfolio.cash -= dollar_amount
    save_paper_portfolio(portfolio)

    trade = PaperTrade(
        date=datetime.date.today().isoformat(),
        ticker=ticker,
        action="BUY",
        shares=round(shares, 6),
        price=round(price, 4),
        value=round(dollar_amount, 4),
        conviction=conviction,
        reason=reason or f"BUY signal conviction={conviction:.1f}",
        portfolio_value_after=round(portfolio.total_value, 4),
    )
    _log_trade(trade)
    try:
        # Scan-time feedback records carry a 0.0 placeholder (AnalysisResult
        # has no price field) — fill the real fill price now
        from core.feedback import update_entry_price
        update_entry_price(ticker, price)
    except Exception as e:
        logger.warning(f"Feedback update_entry_price failed for {ticker}: {e}")
    logger.info(f"Paper BUY: {ticker} {shares:.4f} shares @ ${price:.2f} = ${dollar_amount:.2f}")
    return trade


def _sell_from(
    portfolio: PaperPortfolio,
    ticker: str,
    sell_pct: float = 1.0,
    reason: str = "",
    action_label: str = "SELL_FULL",
) -> Optional[PaperTrade]:
    """
    Sell sell_pct (0-1) of a position, mutating the GIVEN portfolio object.
    Caller is responsible for saving. Keeps daily_update on a single
    load→mutate→save cycle so stop/trim state changes can't be lost to reloads.
    """
    if ticker not in portfolio.positions:
        return None

    pos = portfolio.positions[ticker]
    price = _fetch_price(ticker)
    if price is None:
        price = pos.current_price  # fallback to last known

    shares_to_sell = pos.shares * sell_pct
    proceeds = shares_to_sell * price

    portfolio.cash += proceeds

    if sell_pct >= 0.99:
        del portfolio.positions[ticker]
    else:
        pos.shares = round(pos.shares * (1 - sell_pct), 6)
        pos.cost_basis = round(pos.cost_basis * (1 - sell_pct), 4)
        pos.current_price = price
        portfolio.positions[ticker] = pos

    trade = PaperTrade(
        date=datetime.date.today().isoformat(),
        ticker=ticker,
        action=action_label,
        shares=round(shares_to_sell, 6),
        price=round(price, 4),
        value=round(proceeds, 4),
        conviction=pos.conviction,
        reason=reason,
        portfolio_value_after=round(portfolio.total_value, 4),
    )
    _log_trade(trade)
    ret = (price - pos.entry_price) / pos.entry_price
    logger.info(
        f"Paper {action_label}: {ticker} {shares_to_sell:.4f} shares @ ${price:.2f} "
        f"({ret:+.1%}) → ${proceeds:.2f}"
    )
    if sell_pct >= 0.99:
        # Full exit closes the feedback-loop signal record (WIN/LOSS/SCRATCH +
        # mistake-pattern rescan). Trims keep the record open.
        try:
            from core.feedback import record_exit
            record_exit(ticker, price, reason or action_label, entry_price=pos.entry_price)
        except Exception as e:
            logger.warning(f"Feedback record_exit failed for {ticker}: {e}")
    return trade


def execute_sell(
    ticker: str,
    sell_pct: float = 1.0,
    reason: str = "",
    action_label: str = "SELL_FULL",
) -> Optional[PaperTrade]:
    """Sell sell_pct (0-1) of a position. 1.0 = full exit. Loads and saves."""
    portfolio = load_paper_portfolio()
    trade = _sell_from(portfolio, ticker, sell_pct, reason, action_label)
    if trade:
        save_paper_portfolio(portfolio)
    return trade


# ── Daily update ──────────────────────────────────────────────────────────────

def daily_update() -> dict:
    """
    Run every evening:
    1. Refresh all position prices
    2. Check stop losses → auto-sell if hit
    3. Check profit-taking levels → auto-trim
    4. Snapshot portfolio value for P&L history
    Returns a summary dict.
    """
    from core.stop_loss import check_trimming_levels

    # Single load→mutate→save cycle: sells mutate this same object via
    # _sell_from, so raised stop tiers and trim flags persist to disk.
    portfolio = load_paper_portfolio()
    actions_taken = []
    alerts = []

    for ticker in list(portfolio.positions.keys()):
        pos = portfolio.positions[ticker]
        price = _fetch_price(ticker)
        if price is None:
            continue

        # Split guard: quotes are split-adjusted but the position stores raw
        # shares/entry — an unhandled 4:1 split reads as a -75% crash and fires
        # a false stop exit at a quarter of the real value.
        if pos.current_price and price < pos.current_price * 0.60:
            ratio = _recent_split_ratio(ticker)
            if ratio and ratio > 1:
                _apply_split(pos, ratio)
                alerts.append(f"{ticker}: {ratio:g}:1 split detected — shares/entry/stops adjusted")

        # Update current price and peak price (peak only moves up, never down)
        pos.current_price = price
        if pos.peak_price is None or price > pos.peak_price:
            pos.peak_price = round(price, 4)
        portfolio.positions[ticker] = pos
        ret = pos.return_pct

        # ── Recalculate trailing stop based on peak price ──
        # Trailing stop kicks in at +20% gain and moves up with the stock
        if pos.peak_price and pos.atr_at_entry and ret >= 0.20:
            from core.stop_loss import calculate_stops
            stops = calculate_stops(
                ticker=ticker,
                entry_price=pos.entry_price,
                current_price=price,
                atr=pos.atr_at_entry,
                conviction=int(pos.conviction),
                peak_price=pos.peak_price,
            )
            # Only move stop UP, never down
            if stops.tier3 and (pos.stop_tier3 is None or stops.tier3 > pos.stop_tier3):
                old_stop = pos.stop_tier3
                pos.stop_tier3 = stops.tier3
                pos.stop_tier2 = stops.tier2
                pos.stop_tier1 = stops.tier1
                portfolio.positions[ticker] = pos
                if old_stop is None or stops.tier3 > old_stop + 0.01:
                    alerts.append(
                        f"Trailing stop raised for {ticker}: "
                        f"${old_stop:.2f} → ${stops.tier3:.2f} "
                        f"(peak ${pos.peak_price:.2f}, gain {ret:+.1%}) — {stops.phase.value}"
                    )

        # Check Tier 3 hard stop (uses updated trailing level)
        if pos.stop_tier3 and price <= pos.stop_tier3:
            trade = _sell_from(portfolio, ticker, sell_pct=1.0,
                               reason=f"Tier 3 stop hit ${pos.stop_tier3:.2f} (trailing)",
                               action_label="SELL_STOP")
            if trade:
                actions_taken.append(f"STOP EXIT {ticker}: {ret:+.1%}")
                alerts.append(f"Trailing stop hit on {ticker} — exited at ${price:.2f} ({ret:+.1%})")
            continue

        # Tier 1 alert (just log, don't sell)
        if pos.stop_tier1 and price <= pos.stop_tier1:
            alerts.append(f"STOP ALERT {ticker}: price ${price:.2f} hit Tier 1 ${pos.stop_tier1:.2f}")

        # Profit-taking — each level fires exactly once per position
        trim = check_trimming_levels(pos.entry_price, price, pos.recommended_pct,
                                     already_taken=pos.trims_taken)
        if trim:
            sell_pct = trim["sell_pct"]
            trade = _sell_from(portfolio, ticker, sell_pct=sell_pct,
                               reason=trim["message"],
                               action_label="SELL_TRIM")
            if trade:
                if ticker in portfolio.positions:  # still holding the remainder
                    portfolio.positions[ticker].trims_taken.append(trim["action"])
                actions_taken.append(f"TRIM {int(sell_pct*100)}% {ticker}: {ret:+.1%} — {trim['message']}")

    save_paper_portfolio(portfolio)

    # Snapshot P&L
    _snapshot_pnl(portfolio)

    spy_return = _fetch_spy_return_since(portfolio.start_date) if portfolio.start_date else None

    summary = {
        "date": datetime.date.today().isoformat(),
        "portfolio_value": round(portfolio.total_value, 2),
        "cash": round(portfolio.cash, 2),
        "invested": round(portfolio.invested_value, 2),
        "total_return_pct": round(portfolio.total_return_pct, 4),
        "total_return_dollars": round(portfolio.total_value - portfolio.starting_capital, 2),
        "spy_return_since_start": round(spy_return, 4) if spy_return else None,
        "alpha": round(portfolio.total_return_pct - spy_return, 4) if spy_return else None,
        "n_positions": len(portfolio.positions),
        "actions_taken": actions_taken,
        "alerts": alerts,
        "positions": [
            {
                "ticker": t,
                "entry_price": p.entry_price,
                "current_price": p.current_price,
                "shares": p.shares,
                "market_value": round(p.market_value, 2),
                "return_pct": round(p.return_pct, 4),
                "unrealized_pnl": round(p.unrealized_pnl, 2),
            }
            for t, p in sorted(portfolio.positions.items(),
                                key=lambda x: -x[1].return_pct)
        ],
    }
    return summary


def auto_execute_scan_signals(scan_file: Optional[str] = None) -> list[PaperTrade]:
    """
    Read today's scan results and auto-execute all BUY signals.
    Called automatically at end of each daily scan.
    """
    import glob

    if scan_file is None:
        today = datetime.date.today().isoformat()
        # Try today's scan files
        candidates = (
            list(Path("data").glob(f"scan_{today}.json")) +
            list(Path("data").glob(f"daily_scan_{today}.json"))
        )
        if not candidates:
            logger.info("Paper trading: no scan file for today")
            return []
        scan_file = str(candidates[0])

    try:
        with open(scan_file) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Paper trading: scan file read error: {e}")
        return []

    # Idempotency: the scheduler re-runs a missed scan on next app launch, which
    # would re-execute the morning's BUYs at afternoon prices. One execution per
    # scan date — delete the marker (or reset the portfolio) to force a re-run.
    marker = Path(f"data/.paper_exec_{datetime.date.today().isoformat()}")
    if marker.exists():
        logger.info(f"Paper trading: scan already executed today ({marker}) — skipping. "
                    "Delete the marker file to force re-execution.")
        return []

    from config.settings import settings
    from core.sector_map import to_macro

    trades = []

    # ── Conviction-drop actions first: TRIM_25 / TRIM_50 / EXIT on held names.
    # These come from the drop matrix in the orchestrator and were previously
    # dashboard-only — the paper portfolio held on while the strategy said sell.
    DROP_ACTIONS = {"TRIM_25": 0.25, "TRIM_50": 0.50, "EXIT": 1.0}
    portfolio = load_paper_portfolio()
    for result in data.get("results", []):
        action = result.get("signal")
        if action in DROP_ACTIONS and result["ticker"] in portfolio.positions:
            trade = execute_sell(
                result["ticker"],
                sell_pct=DROP_ACTIONS[action],
                reason=f"Conviction drop matrix: {action} "
                       f"(conviction now {result.get('conviction', 0):.1f})",
                action_label="SELL_FULL" if action == "EXIT" else "SELL_TRIM",
            )
            if trade:
                trades.append(trade)

    # Don't execute new buys if VIX spike
    if data.get("new_buys_paused"):
        logger.info("Paper trading: new buys paused (VIX spike) — no executions")
        return trades

    # ── New buys, with a sequential macro-sector cap. The per-stock gate in the
    # scan sees the portfolio as of scan start; when many BUYs land the same
    # day, exposure has to be re-checked as each buy executes or a single theme
    # absorbs the whole pool (12 buys / 4 medtech on 2026-07-02).
    # Cross-track dedup: the swing book draws from the same $5k pool — holding
    # the same name in both portfolios doubles exposure invisibly.
    swing_held: set[str] = set()
    try:
        from core.growth_paper_trading import load_growth_portfolio
        swing_held = set(load_growth_portfolio().positions.keys())
    except Exception:
        pass

    for result in data.get("results", []):
        if result.get("signal") != "BUY":
            continue

        # Chart verdict gate: the LT chart's whole job is entry timing — buying
        # on "wait" defeats it. Deferred, not dropped: the scan re-runs the
        # chart daily and the buy fires once the entry becomes actionable.
        lt_chart = result.get("lt_chart") or {}
        if lt_chart.get("entry_type") == "wait":
            logger.info(f"Paper: defer {result['ticker']} — chart verdict 'wait', re-checked next scan")
            continue

        if result["ticker"] in swing_held:
            logger.info(f"Paper: skip {result['ticker']} — already held in swing portfolio (unified pool)")
            continue

        portfolio = load_paper_portfolio()  # fresh — includes buys made this loop
        macro = to_macro(result.get("sector", "unknown"))
        limit = settings.max_ai_infra_pct if macro == "technology" else settings.max_sector_pct
        total = portfolio.total_value or 1.0
        exposure = sum(
            p.market_value / total
            for p in portfolio.positions.values()
            if to_macro(p.sector) == macro
        )
        pct = result.get("recommended_pct", 0.05)
        if exposure + pct > limit:
            logger.info(
                f"Paper: skip {result['ticker']} — {macro} at {exposure:.0%}, "
                f"+{pct:.0%} would breach {limit:.0%} cap"
            )
            continue

        trade = execute_buy(
            ticker=result["ticker"],
            conviction=result.get("conviction", 0),
            recommended_pct=pct,
            stop_tier1=result.get("stop_tier1"),
            stop_tier2=result.get("stop_tier2"),
            stop_tier3=result.get("stop_tier3"),
            reason=f"Auto paper trade: BUY signal conviction={result.get('conviction', 0):.1f}",
            sector=result.get("sector", "unknown"),
        )
        if trade:
            trades.append(trade)

    marker.write_text(json.dumps({
        "executed_at": datetime.datetime.now().isoformat(),
        "scan_file": str(scan_file),
        "trades": len(trades),
    }))
    return trades


# ── P&L history ───────────────────────────────────────────────────────────────

def _snapshot_pnl(portfolio: PaperPortfolio) -> None:
    history = _load_pnl_history()
    today = datetime.date.today().isoformat()
    # Replace today's entry if it exists
    history = [e for e in history if e["date"] != today]
    history.append({
        "date": today,
        "total_value": round(portfolio.total_value, 2),
        "cash": round(portfolio.cash, 2),
        "invested": round(portfolio.invested_value, 2),
        "return_pct": round(portfolio.total_return_pct, 4),
        "n_positions": len(portfolio.positions),
    })
    with open(PAPER_PNL_FILE, "w") as f:
        json.dump(history, f, indent=2)


def _load_pnl_history() -> list[dict]:
    if not PAPER_PNL_FILE.exists():
        return []
    try:
        with open(PAPER_PNL_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def get_pnl_history() -> list[dict]:
    return _load_pnl_history()


def reset_paper_portfolio(starting_capital: float = STARTING_CAPITAL) -> None:
    """Wipe paper portfolio and start fresh. Use with care."""
    portfolio = PaperPortfolio(
        cash=starting_capital,
        start_date=datetime.date.today().isoformat(),
        starting_capital=starting_capital,
    )
    save_paper_portfolio(portfolio)
    if PAPER_PNL_FILE.exists():
        os.rename(PAPER_PNL_FILE, str(PAPER_PNL_FILE) + ".bak")
    # A reset is an intentional fresh start — clear execution markers so the
    # next scan (or a re-run of today's) can populate the new portfolio.
    for m in Path("data").glob(".paper_exec_*"):
        m.unlink(missing_ok=True)
    logger.info(f"Paper portfolio reset: starting with ${starting_capital:.2f}")
