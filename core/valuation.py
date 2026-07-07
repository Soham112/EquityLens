"""
Intrinsic Value Gate — Benjamin Graham formula + P/E sector comparison.
Caps conviction when stocks are significantly overvalued.
"""
import logging
from dataclasses import dataclass
from typing import Optional

from core.data_layer import FundamentalsData

logger = logging.getLogger(__name__)

# Sector median P/E ratios (hardcoded, updated periodically)
SECTOR_MEDIAN_PE: dict[str, float] = {
    "technology": 28.0,
    "semiconductors": 28.0,
    "ai_infrastructure": 28.0,
    "memory_storage": 28.0,
    "cloud_saas": 28.0,
    "cybersecurity": 28.0,
    "healthcare": 22.0,
    "financials": 14.0,
    "industrials": 18.0,
    "energy": 12.0,
    "consumer_discretionary": 20.0,
    "consumer": 20.0,
    "materials": 16.0,
    "communication_services": 20.0,
    "defense_aerospace": 18.0,
    "real_estate": 20.0,
}
DEFAULT_PE = 20.0


@dataclass
class ValuationResult:
    ticker: str
    fair_value_low: float       # conservative estimate
    fair_value_high: float      # optimistic estimate
    current_price: float
    margin_of_safety: float     # (fair_value_mid - price) / fair_value_mid — negative = overpriced
    verdict: str                # "UNDERVALUED" | "FAIR" | "OVERVALUED" | "NO_DATA"
    conviction_cap: Optional[float]  # if overvalued: cap conviction at this level
    notes: str


def estimate_fair_value(ticker: str, fundamentals: FundamentalsData, sector: str = "default") -> ValuationResult:
    """
    Estimate intrinsic value using Benjamin Graham's formula or P/E sector comparison.
    Graham formula: fair_value = EPS × (8.5 + 2 × growth_rate)
    """
    current_price = None

    # Try to get current price
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).fast_info
        current_price = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
    except Exception:
        pass

    if current_price is None or current_price <= 0:
        return ValuationResult(
            ticker=ticker,
            fair_value_low=0.0,
            fair_value_high=0.0,
            current_price=0.0,
            margin_of_safety=0.0,
            verdict="NO_DATA",
            conviction_cap=None,
            notes="Could not fetch current price",
        )

    fair_value_mid: Optional[float] = None
    method_used = ""

    # Method 1: Graham formula — requires EPS and growth rate
    eps = None
    growth_rate = None

    # Try to derive EPS from P/E and price
    if fundamentals.pe_ratio and fundamentals.pe_ratio > 0:
        eps = current_price / fundamentals.pe_ratio

    # Estimate growth rate from revenue growth as proxy
    if fundamentals.revenue_growth_yoy is not None:
        growth_rate = fundamentals.revenue_growth_yoy * 100  # convert to % (e.g., 15.0 for 15%)

    if eps is not None and growth_rate is not None and eps > 0 and growth_rate > 0:
        # Benjamin Graham's formula
        fair_value_mid = eps * (8.5 + 2 * growth_rate)
        method_used = f"Graham formula (EPS≈{eps:.2f}, growth={growth_rate:.1f}%)"

    # Method 2: P/E sector comparison
    elif fundamentals.pe_ratio is not None and fundamentals.pe_ratio > 0:
        sector_pe = SECTOR_MEDIAN_PE.get(sector, DEFAULT_PE)
        pe_ratio_stock = fundamentals.pe_ratio
        # Fair value = price × (sector_median_pe / stock_pe)
        fair_value_mid = current_price * (sector_pe / pe_ratio_stock)
        method_used = f"P/E sector comparison (stock P/E={pe_ratio_stock:.1f}, sector median={sector_pe:.1f})"

    if fair_value_mid is None or fair_value_mid <= 0:
        return ValuationResult(
            ticker=ticker,
            fair_value_low=0.0,
            fair_value_high=0.0,
            current_price=current_price,
            margin_of_safety=0.0,
            verdict="NO_DATA",
            conviction_cap=None,
            notes="Insufficient data for valuation (no EPS, P/E, or growth rate)",
        )

    fair_value_low = round(fair_value_mid * 0.85, 2)
    fair_value_high = round(fair_value_mid * 1.15, 2)
    fair_value_mid = round(fair_value_mid, 2)

    margin_of_safety = round((fair_value_mid - current_price) / fair_value_mid, 4)

    # Verdict
    if margin_of_safety > 0.15:
        verdict = "UNDERVALUED"
        conviction_cap = None
        cap_note = ""
    elif margin_of_safety >= -0.10:
        verdict = "FAIR"
        conviction_cap = 9.5
        cap_note = " — conviction capped at 9.5"
    else:
        verdict = "OVERVALUED"
        conviction_cap = 7.0
        cap_note = " — conviction capped at 7.0"

    notes = (
        f"{method_used} | Fair value ${fair_value_low:.0f}–${fair_value_high:.0f} "
        f"(mid ${fair_value_mid:.0f}) | Current ${current_price:.0f} "
        f"| MOS={margin_of_safety:+.1%}{cap_note}"
    )

    logger.debug(f"[Valuation] {ticker}: {verdict} MOS={margin_of_safety:+.1%} via {method_used}")

    return ValuationResult(
        ticker=ticker,
        fair_value_low=fair_value_low,
        fair_value_high=fair_value_high,
        current_price=current_price,
        margin_of_safety=margin_of_safety,
        verdict=verdict,
        conviction_cap=conviction_cap,
        notes=notes,
    )
