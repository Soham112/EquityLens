"""
Momentum Monitor — daily exit signal checker for open swing positions.

Checks these exit conditions on each open swing position:
  1. Stop loss hit       — price fell below ATR stop (already in stop_loss.py)
  2. Momentum stall      — volume drying up + price going flat for 3+ days
  3. Thesis break        — the entry reason is structurally gone
  4. P/E-expansion top   — E16: P/E ran to 2.5x+ its breakout level WHILE earnings
                           growth decelerated (price outran earnings — Minervini topping)

Returns a list of ExitAlert objects for the dashboard and daily scan to act on.
Chart vision is NOT used for exits — it's reserved for entry timing only.
"""
import datetime
import logging
from dataclasses import dataclass
from typing import Optional

import yfinance as yf

logger = logging.getLogger(__name__)

MOMENTUM_STALL_DAYS = 3       # consecutive days of low volume + flat price
VOLUME_STALL_THRESHOLD = 0.80 # volume drops below 80% of 90d avg = stalling
PRICE_FLAT_THRESHOLD = 0.015  # price moving less than 1.5% in either direction per day = flat
PE_EXPANSION_MULT = 2.5       # E16: current P/E >= 2.5x breakout P/E = topping alert zone (Minervini)


@dataclass
class ExitAlert:
    ticker: str
    reason: str           # "MOMENTUM_STALL" | "THESIS_BREAK" | "STOP_LOSS" | "PE_EXPANSION_TOP" | "EARNINGS_PROXIMITY"
    urgency: str          # "IMMEDIATE" | "NEXT_SESSION"
    current_price: float
    entry_price: float
    return_pct: float
    detail: str           # human-readable explanation
    action: str           # what to do


def _fetch_recent_bars(ticker: str, days: int = 10) -> Optional[object]:
    try:
        hist = yf.Ticker(ticker).history(period=f"{days + 5}d")
        if len(hist) < days:
            return None
        return hist.tail(days)
    except Exception as e:
        logger.warning(f"momentum_monitor fetch {ticker}: {e}")
        return None


def check_momentum_stall(ticker: str, vol_90d_avg: Optional[float]) -> tuple[bool, str]:
    """
    Momentum stall: volume consistently below 90d avg AND price not moving.
    Smart money has left — don't stay in a dead trade.
    """
    hist = _fetch_recent_bars(ticker, days=MOMENTUM_STALL_DAYS + 2)
    if hist is None:
        return False, ""

    recent = hist.tail(MOMENTUM_STALL_DAYS)
    close = recent["Close"]
    volume = recent["Volume"]

    # Volume check: all recent days below threshold
    if vol_90d_avg and vol_90d_avg > 0:
        vol_ratios = [v / vol_90d_avg for v in volume]
        all_low_volume = all(r < VOLUME_STALL_THRESHOLD for r in vol_ratios)
    else:
        all_low_volume = False

    # Price check: stock not moving — each day's move < threshold
    price_moves = [
        abs(close.iloc[i] - close.iloc[i - 1]) / close.iloc[i - 1]
        for i in range(1, len(close))
    ]
    all_flat = all(m < PRICE_FLAT_THRESHOLD for m in price_moves)

    if all_low_volume and all_flat:
        avg_vol_ratio = sum(vol_ratios) / len(vol_ratios) if vol_90d_avg else 0
        return True, (
            f"Volume at {avg_vol_ratio:.0%} of 90d avg for {MOMENTUM_STALL_DAYS} days, "
            f"price flat (max move {max(price_moves)*100:.1f}%) — momentum gone"
        )

    return False, ""


def check_thesis_break(ticker: str, signals_fired: list[str], entry_price: float) -> tuple[bool, str]:
    """
    Thesis break: the structural reason for entry is no longer valid.
    Checks: stage deterioration, price fell below entry MA50, kill switch flags.
    """
    try:
        from core.data_layer import fetch_price_data
        p = fetch_price_data(ticker)
        if p is None:
            return False, ""

        # Stage 4 = clear downtrend — thesis is broken regardless of entry reason
        if p.stage == "4":
            return True, f"Stock entered Stage 4 downtrend (price ${p.current_price:.2f})"

        # Price broke below MA50 significantly — structure deteriorated
        if p.current_price < p.price_50d_ma * 0.95:
            drop = (p.price_50d_ma - p.current_price) / p.price_50d_ma
            return True, f"Price {drop:.1%} below MA50 — base structure broken"

        # If entry was based on price_structure (near 52w high) but now far from it
        if "price_structure" in signals_fired:
            if p.week_52_high_pct is not None and p.week_52_high_pct < -0.20:
                return True, f"Price {abs(p.week_52_high_pct)*100:.0f}% below 52w high — breakout thesis broken"

    except Exception as e:
        logger.debug(f"thesis_break check {ticker}: {e}")

    return False, ""


def check_pe_expansion(ticker: str, breakout_pe: Optional[float]) -> tuple[bool, str]:
    """
    E16 — P/E-expansion topping signal (Minervini, ch. 3).

    A superperformance run tops when the price outruns earnings: the P/E balloons
    to 2.5-3x its breakout level WHILE quarterly earnings/revenue growth decelerates.
    The mirror case — P/E flat or expanding while growth keeps pace (Apollo, CKE) —
    is HEALTHY and must NOT fire; that's why deceleration is a required co-condition.

    Fires only when both are true:
      1. current P/E >= PE_EXPANSION_MULT x breakout P/E
      2. revenue_growth_trend == DECELERATING (proxy for earnings deceleration until
         E17 lands a direct quarterly-EPS-growth signal)

    High-precision by design: expansion alone (growth intact) is left to run.
    """
    if not breakout_pe or breakout_pe <= 0:
        return False, ""
    try:
        from core.data_layer import fetch_fundamentals
        f = fetch_fundamentals(ticker)
        if f is None or not f.pe_ratio or f.pe_ratio <= 0:
            return False, ""
        expansion = f.pe_ratio / breakout_pe
        if expansion >= PE_EXPANSION_MULT and f.revenue_growth_trend == "DECELERATING":
            return True, (
                f"P/E {f.pe_ratio:.0f} = {expansion:.1f}x breakout P/E {breakout_pe:.0f} "
                f"with decelerating growth — price outran earnings, classic topping"
            )
    except Exception as e:
        logger.debug(f"pe_expansion check {ticker}: {e}")
    return False, ""


def monitor_open_swings() -> list[ExitAlert]:
    """
    Run all exit checks on open swing positions.
    Called by daily scan every morning.
    Returns list of ExitAlerts — dashboard shows these as action items.
    """
    alerts: list[ExitAlert] = []

    try:
        from core.position_store import load_swing_positions
        positions = list(load_swing_positions("OPEN"))
    except Exception as e:
        logger.warning(f"momentum_monitor: could not load positions: {e}")
        positions = []

    # Also monitor auto-entered swing positions in the growth paper portfolio
    try:
        from types import SimpleNamespace
        from core.growth_paper_trading import load_growth_portfolio
        gp = load_growth_portfolio()
        held = {p.ticker for p in positions}
        for t, gpos in gp.positions.items():
            if t not in held:
                positions.append(SimpleNamespace(
                    ticker=t,
                    entry_price=gpos.entry_price,
                    stop_price=gpos.stop_price,
                    screens_matched=[],
                    breakout_pe=getattr(gpos, "breakout_pe", None),
                ))
    except Exception as e:
        logger.debug(f"momentum_monitor: growth positions skipped: {e}")

    if not positions:
        return alerts

    logger.info(f"[MomentumMonitor] Checking {len(positions)} open swing positions...")

    for pos in positions:
        ticker = pos.ticker
        entry_price = pos.entry_price

        # Get current price
        try:
            hist = _fetch_recent_bars(ticker, days=5)
            if hist is None:
                continue
            current_price = float(hist["Close"].iloc[-1])
        except Exception:
            continue

        return_pct = (current_price - entry_price) / entry_price

        # 1. Stop loss check (defer to existing stop_loss.py — just flag here)
        if current_price <= pos.stop_price:
            alerts.append(ExitAlert(
                ticker=ticker,
                reason="STOP_LOSS",
                urgency="IMMEDIATE",
                current_price=current_price,
                entry_price=entry_price,
                return_pct=return_pct,
                detail=f"Price ${current_price:.2f} hit stop ${pos.stop_price:.2f}",
                action="EXIT IMMEDIATELY — stop loss triggered",
            ))
            continue

        # 2. Momentum stall check
        # Approximate 90d avg volume from position store or fetch fresh
        vol_90d = None
        try:
            full_hist = yf.Ticker(ticker).history(period="100d")
            if len(full_hist) >= 90:
                vol_90d = float(full_hist["Volume"].iloc[-90:].mean())
        except Exception:
            pass

        stall, stall_detail = check_momentum_stall(ticker, vol_90d)
        if stall:
            alerts.append(ExitAlert(
                ticker=ticker,
                reason="MOMENTUM_STALL",
                urgency="NEXT_SESSION",
                current_price=current_price,
                entry_price=entry_price,
                return_pct=return_pct,
                detail=stall_detail,
                action=f"Exit next session — momentum gone. Return so far: {return_pct:+.1%}",
            ))
            continue

        # 3. Thesis break check
        signals = pos.screens_matched or []
        broken, break_detail = check_thesis_break(ticker, signals, entry_price)
        if broken:
            alerts.append(ExitAlert(
                ticker=ticker,
                reason="THESIS_BREAK",
                urgency="NEXT_SESSION",
                current_price=current_price,
                entry_price=entry_price,
                return_pct=return_pct,
                detail=break_detail,
                action=f"Exit next session — entry thesis broken. Return so far: {return_pct:+.1%}",
            ))
            continue

        # 4. P/E-expansion topping (E16) — price outran earnings. High-precision:
        # only fires when the multiple ballooned AND growth decelerated.
        topping, top_detail = check_pe_expansion(ticker, getattr(pos, "breakout_pe", None))
        if topping:
            alerts.append(ExitAlert(
                ticker=ticker,
                reason="PE_EXPANSION_TOP",
                urgency="NEXT_SESSION",
                current_price=current_price,
                entry_price=entry_price,
                return_pct=return_pct,
                detail=top_detail,
                action=f"Topping risk — hunt for sell signals / price weakness. Return so far: {return_pct:+.1%}",
            ))
            continue

        # 5. Earnings proximity — swing positions shouldn't sleepwalk into a print.
        # Not an auto-exit: a decision prompt (exit, trim, or consciously hold).
        try:
            from core.earnings_calendar import get_next_earnings
            import datetime as _dt
            next_e = get_next_earnings(ticker)
            if next_e is not None:
                days_to = (next_e - _dt.date.today()).days
                if 0 <= days_to <= 5:
                    alerts.append(ExitAlert(
                        ticker=ticker,
                        reason="EARNINGS_PROXIMITY",
                        urgency="DECIDE_TODAY",
                        current_price=current_price,
                        entry_price=entry_price,
                        return_pct=return_pct,
                        detail=f"Earnings in {days_to}d ({next_e}). Return so far: {return_pct:+.1%}",
                        action="Decide: exit before print, trim, or consciously hold through earnings",
                    ))
        except Exception:
            pass

    if alerts:
        logger.warning(
            f"[MomentumMonitor] {len(alerts)} exit alert(s): "
            + ", ".join(f"{a.ticker}({a.reason})" for a in alerts)
        )
    else:
        logger.info("[MomentumMonitor] All positions healthy — no exit signals")

    return alerts
