"""
Critic Agent — Detect red flags, kill switches. Veto high Hunter scores if risk present.
Red flags reduce conviction; kill switches → conviction = 0, no exceptions.
"""
import logging
from dataclasses import dataclass

from core.data_layer import FundamentalsData, InsiderData, PriceData

logger = logging.getLogger(__name__)


@dataclass
class CriticResult:
    ticker: str
    red_flags: list[str]          # flag keys (used by conviction system)
    red_flag_labels: list[str]    # human-readable descriptions
    severities: dict[str, str]    # flag_key → "HIGH" | "MEDIUM" | "CRITICAL"
    kill_switch: bool
    caution: bool                  # 2+ non-kill red flags
    summary: str


def run(
    price: PriceData,
    fundamentals: FundamentalsData,
    insider: InsiderData,
    extra_flags: dict | None = None,   # injected by Sentiment agent e.g. {"litigation": True}
) -> CriticResult:
    """
    extra_flags: optional dict from Sentiment/news analysis e.g.
        {"litigation": True, "sec_investigation": True, "analyst_downgrades_3": True}
    """
    red_flags = []
    labels = []
    severities = {}
    extra = extra_flags or {}

    # ── CRITICAL (kill switches) ──
    if extra.get("litigation"):
        red_flags.append("litigation")
        labels.append("Litigation announced")
        severities["litigation"] = "CRITICAL"

    if extra.get("sec_investigation"):
        red_flags.append("sec_investigation")
        labels.append("SEC investigation")
        severities["sec_investigation"] = "CRITICAL"

    if extra.get("auditor_warning") or extra.get("going_concern"):
        red_flags.append("auditor_warning")
        labels.append("Auditor warning / going concern")
        severities["auditor_warning"] = "CRITICAL"

    if extra.get("accounting_restatement"):
        red_flags.append("accounting_restatement")
        labels.append("Accounting restatement")
        severities["accounting_restatement"] = "CRITICAL"

    # ── HIGH ──
    if fundamentals.fcf is not None:
        # Check for 2+ years negative FCF (we flag if negative; multi-year tracked separately)
        if fundamentals.fcf < 0:
            red_flags.append("negative_fcf")
            labels.append(f"Negative FCF (${fundamentals.fcf/1e6:.0f}M)")
            severities["negative_fcf"] = "HIGH"

    if fundamentals.debt_to_equity is not None and fundamentals.debt_to_equity > 2.5:
        red_flags.append("high_debt")
        labels.append(f"D/E ratio {fundamentals.debt_to_equity:.1f} > 2.5")
        severities["high_debt"] = "HIGH"

    if insider.ceo_cfo_selling:
        red_flags.append("insider_selling")
        labels.append("CEO/CFO unusual selling detected")
        severities["insider_selling"] = "HIGH"

    if extra.get("analyst_downgrades_3"):
        red_flags.append("analyst_downgrades")
        labels.append("3+ analyst downgrades in last 30 days")
        severities["analyst_downgrades"] = "HIGH"

    # ── MEDIUM ──
    # Stock up 150%+ without earnings growth is a valuation red flag
    # (Needs historical price; injected via extra_flags)
    if extra.get("price_up_150_no_earnings"):
        red_flags.append("valuation_stretched")
        labels.append("Stock up 150%+ without earnings growth")
        severities["valuation_stretched"] = "MEDIUM"

    if extra.get("customer_concentration_over_40"):
        red_flags.append("customer_concentration")
        labels.append("Customer concentration >40%")
        severities["customer_concentration"] = "MEDIUM"

    # ── DERIVED ──
    kill_switch = any(severities.get(f) == "CRITICAL" for f in red_flags)
    non_kill = [f for f in red_flags if severities.get(f) != "CRITICAL"]
    caution = len(non_kill) >= 2

    if kill_switch:
        summary = f"KILL SWITCH: {', '.join(labels)} — REJECT, conviction = 0"
    elif caution:
        summary = f"CAUTION ({len(non_kill)} flags): {', '.join(labels)} — reduce conviction"
    elif red_flags:
        summary = f"{len(red_flags)} flag(s): {', '.join(labels)}"
    else:
        summary = "No red flags detected"

    return CriticResult(
        ticker=price.ticker,
        red_flags=red_flags,
        red_flag_labels=labels,
        severities=severities,
        kill_switch=kill_switch,
        caution=caution,
        summary=summary,
    )
