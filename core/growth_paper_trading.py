"""
Growth Scout Paper Trading — separate $1,000 paper portfolio for speculative small/mid-cap picks.

Completely isolated from core/paper_trading.py (main $2,000 portfolio).
Data file: data/growth_portfolio.json

Position rules (different from main portfolio):
  - $50 per position (5% of $1,000)
  - Max 10 open positions ($500 deployed, $500 cash reserve)
  - Trailing stop: activates at +30% gain (higher threshold — growth stocks are volatile)
  - Profit take 1: +150% — trim 25%
  - Profit take 2: +300% — trim 40%
  - Hard stop: -25% from entry (growth stocks can move fast)
"""
import datetime
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)

GROWTH_PORTFOLIO_FILE = Path("data/growth_portfolio.json")
GROWTH_TRADES_FILE    = Path("data/growth_trades.json")

# Unified $5,000 paper pool: $3,500 long-term (paper_trading) + $1,500 swing (here)
STARTING_CAPITAL   = 1500.0
POSITION_SIZE      = 250.0      # $ per position
MAX_POSITIONS      = 6
HARD_STOP_PCT      = -0.25      # -25% from entry
TRAIL_ACTIVATES_AT = 0.30       # trailing stop kicks in at +30%
TRAIL_DISTANCE_PCT = 0.15       # trail 15% below peak


@dataclass
class GrowthPosition:
    ticker: str
    entry_price: float
    shares: float
    entry_date: str
    sector: str
    growth_score: float
    signal: str                        # SPECULATIVE BUY
    current_price: float = 0.0
    peak_price: Optional[float] = None
    stop_price: Optional[float] = None # hard stop or trailing stop level
    trims_taken: list[str] = field(default_factory=list)  # each profit-take level fires once

    @property
    def market_value(self) -> float:
        return self.shares * self.current_price

    @property
    def return_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.shares


@dataclass
class GrowthPortfolio:
    starting_capital: float = STARTING_CAPITAL
    cash: float = STARTING_CAPITAL
    start_date: str = ""
    positions: dict[str, GrowthPosition] = field(default_factory=dict)

    @property
    def invested_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        return self.cash + self.invested_value

    @property
    def total_return_pct(self) -> float:
        if self.starting_capital == 0:
            return 0.0
        return (self.total_value - self.starting_capital) / self.starting_capital


def load_growth_portfolio() -> GrowthPortfolio:
    if not GROWTH_PORTFOLIO_FILE.exists():
        return GrowthPortfolio(start_date=datetime.date.today().isoformat())
    try:
        data = json.loads(GROWTH_PORTFOLIO_FILE.read_text())
        positions = {
            t: GrowthPosition(**p)
            for t, p in data.get("positions", {}).items()
        }
        return GrowthPortfolio(
            starting_capital=data.get("starting_capital", STARTING_CAPITAL),
            cash=data.get("cash", STARTING_CAPITAL),
            start_date=data.get("start_date", ""),
            positions=positions,
        )
    except Exception as e:
        logger.error(f"Failed to load growth portfolio: {e}")
        return GrowthPortfolio(start_date=datetime.date.today().isoformat())


def _save_growth_portfolio(p: GrowthPortfolio) -> None:
    GROWTH_PORTFOLIO_FILE.parent.mkdir(exist_ok=True)
    data = {
        "starting_capital": p.starting_capital,
        "cash": round(p.cash, 4),
        "start_date": p.start_date,
        "positions": {t: asdict(pos) for t, pos in p.positions.items()},
    }
    GROWTH_PORTFOLIO_FILE.write_text(json.dumps(data, indent=2))


def _log_trade(ticker: str, action: str, price: float, shares: float,
               reason: str, pnl: float = 0.0) -> None:
    GROWTH_TRADES_FILE.parent.mkdir(exist_ok=True)
    trades = []
    if GROWTH_TRADES_FILE.exists():
        try:
            trades = json.loads(GROWTH_TRADES_FILE.read_text())
        except Exception:
            pass
    trades.append({
        "date": datetime.datetime.now().isoformat(),
        "ticker": ticker,
        "action": action,
        "price": round(price, 4),
        "shares": round(shares, 4),
        "value": round(price * shares, 2),
        "reason": reason,
        "pnl": round(pnl, 2),
    })
    GROWTH_TRADES_FILE.write_text(json.dumps(trades, indent=2))


def _fetch_price(ticker: str) -> Optional[float]:
    try:
        hist = yf.Ticker(ticker).history(period="2d")
        return float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception:
        return None


def execute_buy(ticker: str, sector: str, growth_score: float,
                signal: str = "SPECULATIVE BUY",
                size_dollars: Optional[float] = None) -> bool:
    """
    Buy into ticker. Size defaults to an equal slot (total_value / MAX_POSITIONS);
    pass size_dollars to override with setup-quality-weighted sizing.
    Skips if: already holding, max positions reached, insufficient cash.
    """
    p = load_growth_portfolio()

    if ticker in p.positions:
        logger.info(f"Growth: already holding {ticker} — skip buy")
        return False
    if len(p.positions) >= MAX_POSITIONS:
        logger.info(f"Growth: max {MAX_POSITIONS} positions reached — skip {ticker}")
        return False

    # Compounding sizing: total value (cash + open positions) split across all slots.
    # Profits from exits flow back into cash, so slot size grows as the pool grows —
    # and shrinks after losses. POSITION_SIZE only acts as the floor for a viable trade.
    position_size = round(size_dollars if size_dollars else p.total_value / MAX_POSITIONS, 2)
    position_size = min(position_size, p.cash)  # never buy more than available cash
    if position_size < POSITION_SIZE * 0.4:     # below ~$100 a swing trade is pointless
        logger.info(f"Growth: slot size ${position_size:.0f} too small (cash ${p.cash:.0f}) — skip {ticker}")
        return False

    price = _fetch_price(ticker)
    if not price:
        logger.warning(f"Growth: could not fetch price for {ticker}")
        return False

    shares = round(position_size / price, 4)
    stop = round(price * (1 + HARD_STOP_PCT), 4)

    p.positions[ticker] = GrowthPosition(
        ticker=ticker,
        entry_price=price,
        shares=shares,
        entry_date=datetime.date.today().isoformat(),
        sector=sector,
        growth_score=growth_score,
        signal=signal,
        current_price=price,
        peak_price=price,
        stop_price=stop,
    )
    if not p.start_date:
        p.start_date = datetime.date.today().isoformat()
    p.cash = round(p.cash - position_size, 4)
    _save_growth_portfolio(p)
    _log_trade(ticker, "BUY", price, shares, f"Growth score {growth_score:.1f}")
    logger.info(f"Growth BUY: {ticker} {shares} shares @ ${price:.2f} = ${position_size:.0f}")
    return True


def auto_enter_swing_signals(swing_signals: list) -> list[str]:
    """
    Auto-enter validated swing setups from the daily scan.

    Entry criteria (all required):
      - 4+/7 signals fired
      - Chart confirms an actionable entry (breakout/pullback/bounce — not "wait")
      - Risk/reward >= 2.0
      - Current price within 2% of the entry zone (not chasing)

    Uses the chart-derived stop (S1 - 0.5*ATR) instead of the -25% default.
    Returns list of action strings for logging.
    """
    from config.settings import settings
    from core.sector_map import to_macro

    # Idempotency: one auto-entry pass per day (scheduler re-runs a missed scan
    # on next launch — afternoon prices, same signals). Delete marker to force.
    marker = GROWTH_PORTFOLIO_FILE.parent / f".swing_enter_{datetime.date.today().isoformat()}"
    if marker.exists():
        logger.info(f"Swing auto-entry already ran today ({marker}) — skipping.")
        return []

    # Cross-track dedup: don't double up on a name the LT portfolio already holds
    lt_held: set[str] = set()
    try:
        from core.paper_trading import load_paper_portfolio
        lt_held = set(load_paper_portfolio().positions.keys())
    except Exception:
        pass

    actions: list[str] = []

    # ── Exploration mode: adaptive selection gates ──
    # Selection gates (signals / R/R / entry zone) run loose, tag entries with the
    # strict gates they'd have failed, and let per-gate cohort evidence re-tighten
    # them (feedback.adapt_swing_gates — one-way ratchet). Risk controls below
    # (earnings blackout, sector slots, risk cap, stops) are NEVER loosened.
    exploring = getattr(settings, "swing_entry_mode", "strict") == "exploration"
    gate_state = {}
    if exploring:
        try:
            from core.feedback import adapt_swing_gates, load_gate_state
            for a in adapt_swing_gates():
                actions.append(f"[GateAdapt] {a}")
            gate_state = load_gate_state()
        except Exception as e:
            logger.warning(f"Gate adaptation failed — falling back to strict gates: {e}")
            exploring = False

    def _loose(gate: str) -> bool:
        return exploring and gate_state.get(gate) == "loose"

    min_signals = settings.swing_explore_min_signals if _loose("signals") else settings.swing_strict_min_signals
    min_rr = settings.swing_explore_min_rr if _loose("risk_reward") else settings.swing_strict_min_rr
    zone_tol = settings.swing_explore_zone_tolerance if _loose("entry_zone") else settings.swing_strict_zone_tolerance

    for s in swing_signals:
        score = getattr(s, "signals_score", 0)
        entry_type = getattr(s, "entry_type", None)
        rr = getattr(s, "risk_reward", None) or 0.0
        if score < min_signals or entry_type in (None, "wait") or rr < min_rr:
            continue

        # Tag which STRICT gates this entry would have failed — rides into the
        # feedback record so cohort stats can judge each loosened gate later
        strict_tags: list[str] = []
        if score < settings.swing_strict_min_signals:
            strict_tags.append("strict:signals")
        if rr < settings.swing_strict_min_rr:
            strict_tags.append("strict:risk_reward")

        if s.ticker in lt_held:
            actions.append(f"{s.ticker}: setup valid but already held in LT portfolio — skip (unified pool)")
            continue

        # Earnings blackout: a swing entered days before a print is a coin flip
        # on the report, not a technical setup. The 14-35d catalyst window is a
        # bonus signal; anything inside the blackout is uninvestable for entry.
        try:
            from core.earnings_calendar import get_next_earnings
            next_e = get_next_earnings(s.ticker)
            if next_e is not None:
                days_to = (next_e - datetime.date.today()).days
                if 0 <= days_to < settings.swing_earnings_blackout_days:
                    actions.append(f"{s.ticker}: setup valid but earnings in {days_to}d — blackout, no entry")
                    continue
        except Exception:
            pass

        entry_low = getattr(s, "entry_zone_low", None)
        entry_high = getattr(s, "entry_zone_high", None)
        price = getattr(s, "price", None)
        if price and entry_high and price > entry_high * (1 + zone_tol):
            actions.append(f"{s.ticker}: setup valid but price ${price:.2f} above entry zone — not chasing")
            continue
        if price and entry_high and price > entry_high * (1 + settings.swing_strict_zone_tolerance):
            strict_tags.append("strict:entry_zone")
        # Below the zone is not a discount — it means the setup isn't at the
        # entry yet (or support already failed). Only the upside was guarded before.
        if price and entry_low and price < entry_low * 0.98:
            actions.append(f"{s.ticker}: setup valid but price ${price:.2f} below entry zone — knife, no entry")
            continue

        p = load_growth_portfolio()

        # Sector slots: max N of the 6 slots in one macro sector
        macro = to_macro(getattr(s, "sector", "swing"))
        same_sector = sum(1 for pos in p.positions.values() if to_macro(pos.sector) == macro)
        if same_sector >= settings.swing_max_per_macro_sector:
            actions.append(f"{s.ticker}: setup valid but already {same_sector} {macro} swings open — sector slots full")
            continue

        # Setup-quality-weighted size: base slot scaled by signal strength and R/R.
        # Weakest valid setup (4/7, R/R 2.0) → ~0.89x slot; strongest → 1.25x cap.
        base_slot = p.total_value / MAX_POSITIONS
        quality = 0.6 * (score / 7.0) + 0.4 * min(rr, 4.0) / 4.0
        size = round(min(base_slot * (0.35 + quality), base_slot * 1.25), 2)

        # Mistake-pattern penalty: setups matching a learned losing pattern get
        # downsized (−15% per penalty point, capped −22.5%), never hard-blocked —
        # the entry gates above stay the sole yes/no authority.
        try:
            from core.feedback import mistake_conviction_penalty
            mp, mp_reasons = mistake_conviction_penalty(
                hunter_score=round(float(score) * 10.0 / 7.0, 1),
                signal_type="SWING",
                pattern=getattr(s, "pattern", None),
                pattern_confidence=getattr(s, "pattern_confidence", None),
                n_screens=len(getattr(s, "signals_fired", []) or []) or None,
            )
            if mp > 0:
                size = round(size * (1 - 0.15 * mp), 2)
                actions.append(f"{s.ticker}: {'; '.join(mp_reasons)} — size reduced to ${size:.0f}")
        except Exception as e:
            logger.warning(f"Mistake-pattern check failed for {s.ticker}: {e}")

        # Probation sizing: entries that fail any strict gate ride at half size —
        # more data points per dollar of risk while the cohort evidence accumulates
        if strict_tags:
            size = round(size * settings.swing_probation_size_mult, 2)

        # Risk-per-trade cap: with the chart stop known, don't size a position
        # whose stop-out would cost more than swing_max_risk_per_trade of the pool.
        chart_stop = getattr(s, "stop_level", None)
        if chart_stop and price and price > chart_stop:
            risk_frac = (price - chart_stop) / price
            max_risk_dollars = p.total_value * settings.swing_max_risk_per_trade
            risk_cap_size = round(max_risk_dollars / risk_frac, 2)
            if size > risk_cap_size:
                actions.append(
                    f"{s.ticker}: size ${size:.0f} → ${risk_cap_size:.0f} "
                    f"(stop {risk_frac:.0%} below entry, max {settings.swing_max_risk_per_trade:.1%} risk)"
                )
                size = risk_cap_size

        if execute_buy(s.ticker, getattr(s, "sector", "swing"), float(score),
                       signal="SWING AUTO", size_dollars=size):
            # Override the default -25% stop with the chart-derived stop
            p = load_growth_portfolio()
            if chart_stop and s.ticker in p.positions:
                p.positions[s.ticker].stop_price = round(float(chart_stop), 4)
                _save_growth_portfolio(p)
            # Open a feedback-loop record with the actual fill price so the
            # exit can be scored (WIN/LOSS + mistake patterns) later
            try:
                from core.feedback import log_signal
                fill = p.positions[s.ticker].entry_price if s.ticker in p.positions else (price or 0.0)
                log_signal(
                    ticker=s.ticker,
                    # strict:* tags ride with the fired screens — gate_cohort_report
                    # groups closed trades by them to judge each loosened gate
                    screens_matched=list(getattr(s, "signals_fired", []) or []) + strict_tags,
                    signal_type="SWING",
                    entry_price=fill,
                    # normalize 0-7 signal score to hunter's 0-10 scale so the
                    # low_hunter_swing pattern threshold means the same thing
                    hunter_score=round(float(score) * 10.0 / 7.0, 1),
                    rsi_at_entry=0.0,
                    pattern=getattr(s, "pattern", None) or "none",
                    pattern_confidence=getattr(s, "pattern_confidence", None) or 0.0,
                )
            except Exception as e:
                logger.warning(f"Feedback log_signal failed for {s.ticker}: {e}")
            probation = f" | PROBATION 0.5x ({', '.join(strict_tags)})" if strict_tags else ""
            actions.append(
                f"{s.ticker}: SWING AUTO-ENTRY {score}/7 | {entry_type} | R/R {rr:.1f}x | stop ${chart_stop or 0:.2f}{probation}"
            )
    marker.write_text(datetime.datetime.now().isoformat())
    if actions:
        for a in actions:
            logger.info(f"[SwingAuto] {a}")
    return actions


def _record_feedback_exit(ticker: str, price: float, reason: str,
                          entry_price: Optional[float] = None) -> None:
    """Close the feedback-loop signal record on a full exit (WIN/LOSS/SCRATCH
    scoring + mistake-pattern rescan). Logged, never fatal to the exit itself."""
    try:
        from core.feedback import record_exit
        record_exit(ticker, price, reason, entry_price=entry_price)
    except Exception as e:
        logger.warning(f"Feedback record_exit failed for {ticker}: {e}")


def daily_update() -> list[str]:
    """
    Refresh prices, update trailing stops, check exit triggers.
    Returns list of alert strings for dashboard display.
    """
    p = load_growth_portfolio()
    alerts: list[str] = []
    to_exit: list[str] = []

    for ticker, pos in p.positions.items():
        price = _fetch_price(ticker)
        if not price:
            continue

        # Split guard — see paper_trading.daily_update: an unadjusted split
        # reads as a crash and fires a false stop.
        if pos.current_price and price < pos.current_price * 0.60:
            try:
                from core.paper_trading import _recent_split_ratio
                ratio = _recent_split_ratio(ticker)
            except Exception:
                ratio = None
            if ratio and ratio > 1:
                pos.shares = round(pos.shares * ratio, 4)
                pos.entry_price = round(pos.entry_price / ratio, 4)
                for attr in ("stop_price", "peak_price"):
                    v = getattr(pos, attr)
                    if v:
                        setattr(pos, attr, round(v / ratio, 4))
                alerts.append(f"Growth {ticker}: {ratio:g}:1 split detected — position adjusted")

        pos.current_price = price
        ret = pos.return_pct

        # Update peak price
        if pos.peak_price is None or price > pos.peak_price:
            pos.peak_price = round(price, 4)

        # Trailing stop: activates at +30%, trails 15% below peak
        if ret >= TRAIL_ACTIVATES_AT and pos.peak_price:
            trail_stop = round(pos.peak_price * (1 - TRAIL_DISTANCE_PCT), 4)
            if pos.stop_price is None or trail_stop > pos.stop_price:
                pos.stop_price = trail_stop
                alerts.append(f"Growth {ticker}: trailing stop raised to ${trail_stop:.2f} (peak ${pos.peak_price:.2f})")

        # Check stop hit
        if pos.stop_price and price <= pos.stop_price:
            reason = "TRAIL_STOP" if ret >= TRAIL_ACTIVATES_AT else "HARD_STOP"
            alerts.append(f"GROWTH STOP HIT {ticker}: ${price:.2f} ≤ stop ${pos.stop_price:.2f} ({ret:+.0%}) — EXIT")
            to_exit.append((ticker, price, reason))

        # Profit takes — higher level checked FIRST (the old order made +300%
        # unreachable behind the +150% elif), and each level fires exactly once.
        elif ret >= 3.00 and "TRIM_40" not in pos.trims_taken:
            trim_shares = round(pos.shares * 0.40, 4)
            pnl = trim_shares * (price - pos.entry_price)
            p.cash += round(trim_shares * price, 4)
            pos.shares -= trim_shares
            pos.trims_taken.append("TRIM_40")
            _log_trade(ticker, "TRIM_40", price, trim_shares, "+300% profit take", pnl)
            alerts.append(f"Growth TRIM {ticker}: sold 40% at ${price:.2f} (+300%) — locked ${pnl:.0f}")

        # Profit take at +150%
        elif ret >= 1.50 and "TRIM_25" not in pos.trims_taken:
            trim_shares = round(pos.shares * 0.25, 4)
            pnl = trim_shares * (price - pos.entry_price)
            p.cash += round(trim_shares * price, 4)
            pos.shares -= trim_shares
            pos.trims_taken.append("TRIM_25")
            _log_trade(ticker, "TRIM_25", price, trim_shares, "+150% profit take", pnl)
            alerts.append(f"Growth TRIM {ticker}: sold 25% at ${price:.2f} (+150%) — locked ${pnl:.0f}")

    # Execute exits
    for ticker, price, reason in to_exit:
        pos = p.positions.pop(ticker)
        proceeds = round(pos.shares * price, 4)
        pnl = proceeds - (pos.shares * pos.entry_price)
        p.cash += proceeds
        _log_trade(ticker, "SELL", price, pos.shares, reason, pnl)
        _record_feedback_exit(ticker, price, reason, entry_price=pos.entry_price)
        logger.info(f"Growth EXIT {ticker} @ ${price:.2f} ({reason}) — PnL ${pnl:.0f}")

    _save_growth_portfolio(p)
    return alerts


def execute_exit_alerts(alerts: list) -> list[str]:
    """
    Auto-execute momentum-stall and thesis-break exits from monitor_open_swings().
    Stops are already handled by daily_update; earnings proximity stays a
    decision prompt for the user — those two reasons are deliberately excluded.
    Returns action strings for logging.
    """
    AUTO_EXIT_REASONS = {"MOMENTUM_STALL", "THESIS_BREAK"}
    p = load_growth_portfolio()
    actions: list[str] = []
    changed = False

    for a in alerts:
        ticker = getattr(a, "ticker", None)
        reason = getattr(a, "reason", None)
        if not ticker or reason not in AUTO_EXIT_REASONS or ticker not in p.positions:
            continue
        pos = p.positions.pop(ticker)
        price = _fetch_price(ticker) or pos.current_price or pos.entry_price
        proceeds = round(pos.shares * price, 4)
        pnl = proceeds - pos.shares * pos.entry_price
        p.cash += proceeds
        changed = True
        _log_trade(ticker, "SELL", price, pos.shares, reason, pnl)
        _record_feedback_exit(ticker, price, reason, entry_price=pos.entry_price)
        ret = (price - pos.entry_price) / pos.entry_price if pos.entry_price else 0.0
        actions.append(f"{ticker}: AUTO-EXIT {reason} @ ${price:.2f} ({ret:+.1%}, PnL ${pnl:.0f})")
        logger.info(f"Growth AUTO-EXIT {ticker} ({reason}) @ ${price:.2f} — PnL ${pnl:.0f}")

    if changed:
        _save_growth_portfolio(p)
    return actions


def load_growth_trades() -> list[dict]:
    if not GROWTH_TRADES_FILE.exists():
        return []
    try:
        return json.loads(GROWTH_TRADES_FILE.read_text())
    except Exception:
        return []
