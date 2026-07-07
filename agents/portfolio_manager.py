"""
Portfolio Manager Agent — Position sizing, concentration limits, rebalancing [GAP 8].
Implements correlation monitoring, reentry rules [GAP 4], anti-whipsaw logic.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from agents.validator import ValidatorResult
from config.settings import settings
from core.correlation import check_correlation_limit
from core.regime_detector import MarketRegime, RegimeResult
from core.stop_loss import StopLevels, calculate_stops

logger = logging.getLogger(__name__)


@dataclass
class Position:
    ticker: str
    entry_price: float
    current_price: float
    shares: float
    conviction: float
    sector: str
    atr: float
    peak_price: float = 0.0
    exit_count_90d: int = 0     # how many times exited in last 90 days (whipsaw detector)


@dataclass
class PositionSizeResult:
    ticker: str
    recommended_pct: float      # % of portfolio
    recommended_dollars: float  # assuming portfolio_value input
    position_size_label: str    # "FULL" | "HALF" | "NO_BUY"
    rationale: str
    stops: Optional[StopLevels]
    alerts: list[str]


@dataclass
class PortfolioState:
    positions: dict[str, Position] = field(default_factory=dict)
    cash_pct: float = 1.0
    portfolio_value: float = 100_000.0

    def sector_allocation(self) -> dict[str, float]:
        total = self.portfolio_value
        alloc: dict[str, float] = {}
        for pos in self.positions.values():
            pct = (pos.shares * pos.current_price) / total
            alloc[pos.sector] = alloc.get(pos.sector, 0) + pct
        return alloc

    def is_whipsaw_candidate(self, ticker: str) -> bool:
        pos = self.positions.get(ticker)
        if pos and pos.exit_count_90d >= 2:
            return True
        return False


def recommend_position_size(
    validator: ValidatorResult,
    portfolio: PortfolioState,
    regime: RegimeResult,
    sector: str,
    price: float,
    atr: float,
) -> PositionSizeResult:
    ticker = validator.ticker
    conviction = validator.conviction.conviction
    data_confidence = validator.conviction.data_confidence
    alerts = []

    # No buy conditions
    if validator.conviction.signal != "BUY":
        return PositionSizeResult(
            ticker=ticker, recommended_pct=0, recommended_dollars=0,
            position_size_label="NO_BUY",
            rationale=f"Signal={validator.conviction.signal}; not a buy",
            stops=None, alerts=[],
        )

    # Whipsaw check [GAP 4]
    if portfolio.is_whipsaw_candidate(ticker):
        alerts.append(f"{ticker}: WHIPSAW CANDIDATE — requires manual approval")
        conviction = min(conviction, 7)  # cap at 7 for 90 days

    # Continuous conviction-weighted sizing — a 10.0 earns more than an 8.3.
    # Interpolates within each tier instead of giving every stock in it the same pct.
    if conviction >= 8 and data_confidence >= 8:
        # conviction 8.0 → 5.5%, 10.0 → max_position_high_conviction (7%)
        span = settings.max_position_high_conviction - 0.055
        base_pct = 0.055 + span * min((conviction - 8.0) / 2.0, 1.0)
        label = "FULL"
    elif conviction >= 7 and data_confidence >= 7:
        # conviction 7.0 → 3%, 8.0 → max_position_med_conviction (4%)
        span = settings.max_position_med_conviction - 0.03
        base_pct = 0.03 + span * min(conviction - 7.0, 1.0)
        label = "MEDIUM"
    else:
        base_pct = 0.02   # 2% for borderline buys
        label = "SMALL"

    # Regime adjustment
    base_pct = min(base_pct, regime.max_position_pct)

    # Concentration limits — compare at MACRO sector level, otherwise sibling
    # microsectors (pharma / biotech / medtech_devices) each get their own 25%
    # and a single theme can quietly absorb half the portfolio.
    from core.sector_map import to_macro
    macro = to_macro(sector)
    total = portfolio.portfolio_value or 1.0
    macro_alloc: dict[str, float] = {}
    for pos in portfolio.positions.values():
        m = to_macro(pos.sector)
        macro_alloc[m] = macro_alloc.get(m, 0) + (pos.shares * pos.current_price) / total
    current_sector_pct = macro_alloc.get(macro, 0)

    sector_limit = settings.max_sector_pct
    if macro == "technology":
        # AI/semis is the deliberate overweight in this strategy
        sector_limit = settings.max_ai_infra_pct

    if current_sector_pct + base_pct > sector_limit:
        available = max(0, sector_limit - current_sector_pct)
        alerts.append(f"Sector {macro} at {current_sector_pct:.0%}; limiting to {available:.0%}")
        base_pct = available

    if base_pct < 0.005:
        return PositionSizeResult(
            ticker=ticker, recommended_pct=0, recommended_dollars=0,
            position_size_label="NO_BUY",
            rationale=f"Sector concentration limit reached for {sector}",
            stops=None, alerts=alerts,
        )

    # Correlation limit check [GAP 12]
    held_pcts = {t: (p.shares * p.current_price) / portfolio.portfolio_value
                 for t, p in portfolio.positions.items()}
    corr_check = check_correlation_limit(
        candidate_ticker=ticker,
        candidate_size_pct=base_pct,
        held_positions=held_pcts,
        use_dynamic=False,  # preset-only during scoring (fast); dynamic on dashboard
    )
    alerts.extend(corr_check.alerts)
    if not corr_check.allowed:
        return PositionSizeResult(
            ticker=ticker, recommended_pct=0, recommended_dollars=0,
            position_size_label="NO_BUY",
            rationale=corr_check.rationale,
            stops=None, alerts=alerts,
        )
    if corr_check.capped_pct is not None:
        base_pct = corr_check.capped_pct

    # Cash floor check
    if portfolio.cash_pct - base_pct < settings.min_cash_pct:
        base_pct = max(0, portfolio.cash_pct - settings.min_cash_pct)
        alerts.append(f"Cash floor {settings.min_cash_pct:.0%} enforced")

    # Calculate stops
    entry_price = price
    stops = calculate_stops(
        ticker=ticker,
        entry_price=entry_price,
        current_price=entry_price,
        atr=atr,
        conviction=int(conviction),
    )

    # Risk-per-trade cap: conviction sets exposure, but two 7% positions with
    # stops 7% vs 28% below entry risk wildly different dollars. Cap the size
    # so a tier-3 stop hit loses at most max_risk_per_trade of the portfolio.
    if stops.tier3 and entry_price > 0:
        stop_dist = (entry_price - stops.tier3) / entry_price
        if stop_dist > 0:
            risk_cap = settings.max_risk_per_trade / stop_dist
            if base_pct > risk_cap:
                alerts.append(
                    f"Risk cap: {base_pct:.1%} → {risk_cap:.1%} position "
                    f"(stop {stop_dist:.0%} below entry, max {settings.max_risk_per_trade:.1%} risk/trade)"
                )
                base_pct = round(risk_cap, 4)

    dollar_amount = portfolio.portfolio_value * base_pct

    rationale = (
        f"Conviction={conviction:.1f}, DataConf={data_confidence:.1f}, "
        f"Regime={regime.regime.value}, Sector={sector} at {current_sector_pct:.0%} → "
        f"{base_pct:.1%} position (${dollar_amount:,.0f})"
    )

    return PositionSizeResult(
        ticker=ticker,
        recommended_pct=round(base_pct, 3),
        recommended_dollars=round(dollar_amount, 0),
        position_size_label=label,
        rationale=rationale,
        stops=stops,
        alerts=alerts,
    )


def check_portfolio_concentration(portfolio: PortfolioState) -> list[str]:
    alerts = []
    sector_alloc = portfolio.sector_allocation()
    for sector, pct in sector_alloc.items():
        limit = settings.max_ai_infra_pct if sector in ("ai_infrastructure", "semiconductors") else settings.max_sector_pct
        if pct > limit:
            alerts.append(f"CONCENTRATION ALERT: {sector} at {pct:.0%} exceeds {limit:.0%} limit")
    return alerts


def check_rebalancing_needed(portfolio: PortfolioState, positions_with_conviction: dict[str, float]) -> list[str]:
    """Quarterly: if conviction dropped >3 points, trim 30-50%."""
    actions = []
    for ticker, current_conviction in positions_with_conviction.items():
        pos = portfolio.positions.get(ticker)
        if pos and current_conviction < pos.conviction - 3:
            actions.append(
                f"TRIM {ticker}: conviction dropped from {pos.conviction:.0f} to {current_conviction:.0f} "
                f"— trim 30-50%"
            )
    return actions
