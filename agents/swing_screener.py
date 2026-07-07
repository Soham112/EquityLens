"""
Swing Screener Agent — identifies short-to-medium-term swing trade opportunities.

Pipeline:
  1. ScreenCandidate (passed screener) → Haiku vision first-pass (cheap, ~$0.004)
  2. If Haiku finds pattern with confidence > 0.6 → escalate to Sonnet (full analysis)
  3. Combine vision + numerical signals → SwingSignal

Signal types produced:
  SWING     — technical pattern, Hunter < 7, fundamentals secondary
              exit: hard target at 15–20%
  MOMENTUM  — pattern + Hunter 7+, fundamentals supportive
              exit: trailing stop 10–15% below peak, target can be 50%+

Position promotion path:
  SWING → MOMENTUM → LONG_TERM
  Promotion triggered when: price hits initial target AND structure still ADVANCING
  at vision re-scan. Promotion requires Hunter score check at that point.
"""
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from core.chart_renderer import render_chart
from core.llm_client import HAIKU, SONNET, get_client
from core.screener import ScreenCandidate
from core import vision_cache
from agents.chart_vision import ChartVisionResult, run as full_vision_run

logger = logging.getLogger(__name__)

# Swing-relevant patterns only — tighter list than the full 19 for Haiku pass
SWING_PATTERNS = [
    "bull_flag",
    "breakout",
    "falling_wedge",
    "ascending_triangle",
    "cup_and_handle",
    "double_bottom",
    "base_building",
    "none",
]

_HAIKU_SYSTEM = """\
You are a technical analyst screening charts for swing trade setups.
You will receive a daily candlestick chart with MA50, MA200, volume, and RSI.
Your only job: does this chart show a clear swing trade pattern RIGHT NOW?
Be strict — only flag high-confidence setups. When in doubt, return "none".
Output ONLY valid JSON. No extra text.
"""

_HAIKU_USER = """\
Ticker: {ticker}

Does this chart show a clear swing pattern? Choose from: {patterns}

Respond with ONLY this JSON:
{{
  "pattern": "<one value from list above>",
  "confidence": <0.0-1.0>,
  "reason": "<one sentence max>"
}}
"""


@dataclass
class ExitPlan:
    hard_target_pct: Optional[float]    # e.g. 0.18 = take profit at +18%
    trailing_stop_pct: Optional[float]  # e.g. 0.12 = trail 12% below peak
    stop_loss_pct: float                # e.g. 0.07 = stop out at -7%
    invalidation_note: str              # "close below $142 = pattern failed"


@dataclass
class SwingSignal:
    ticker: str
    sector: str
    signal_type: str            # "SWING" | "MOMENTUM"
    screens_matched: list[str]  # from ScreenCandidate
    pattern: str
    pattern_confidence: float
    price_structure: str        # from full vision (ADVANCING / BASING / etc.)
    ma_crossover_type: str
    entry_price: float
    exit_plan: ExitPlan
    pattern_thesis: str
    actionable_lean: str        # "BUY" | "WATCH" | "AVOID"
    chart_score_delta: float
    haiku_used: bool            # True if Haiku was first-pass; False if Sonnet direct
    sonnet_escalated: bool      # True if Haiku triggered Sonnet escalation
    error: Optional[str] = None
    promotion_eligible: bool = False  # True if Hunter + vision suggest MOMENTUM track


def _null_signal(candidate: ScreenCandidate, reason: str) -> SwingSignal:
    return SwingSignal(
        ticker=candidate.ticker,
        sector=candidate.sector,
        signal_type="SWING",
        screens_matched=candidate.screens_matched,
        pattern="none",
        pattern_confidence=0.0,
        price_structure="BASING",
        ma_crossover_type="none",
        entry_price=candidate.price,
        exit_plan=ExitPlan(
            hard_target_pct=0.18,
            trailing_stop_pct=None,
            stop_loss_pct=0.07,
            invalidation_note="No pattern found",
        ),
        pattern_thesis="No swing setup identified.",
        actionable_lean="WATCH",
        chart_score_delta=0.0,
        haiku_used=False,
        sonnet_escalated=False,
        error=reason,
    )


def _haiku_first_pass(ticker: str, png_bytes: bytes) -> tuple[str, float, str]:
    """
    Fast Haiku vision pass — just: is there a swing pattern here?
    Returns (pattern, confidence, reason). Cost: ~$0.004/chart.
    """
    client = get_client()
    if client is None:
        return "none", 0.0, "no_api_key"

    b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
    user_text = _HAIKU_USER.format(
        ticker=ticker,
        patterns=", ".join(SWING_PATTERNS),
    )

    try:
        response = client.messages.create(
            model=HAIKU,
            max_tokens=150,
            temperature=0.1,
            system=[{"type": "text", "text": _HAIKU_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64},
                    },
                    {"type": "text", "text": user_text},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        return (
            data.get("pattern", "none"),
            float(data.get("confidence", 0.0)),
            data.get("reason", ""),
        )
    except Exception as e:
        logger.warning(f"[SwingScreener] Haiku pass failed for {ticker}: {e}")
        return "none", 0.0, str(e)


def _build_exit_plan(
    entry_price: float,
    atr: Optional[float],
    pattern: str,
    signal_type: str,
    confidence: float,
) -> ExitPlan:
    """
    Exit rules:
    - SWING (low conviction): hard profit target, tight stop
    - MOMENTUM (high conviction): trailing stop, no hard cap (let winners run)
    Stop loss is always ATR-based — 1.5× ATR below entry for swings,
    2× ATR for momentum (more room to breathe).
    """
    atr_pct = (atr / entry_price) if atr and entry_price > 0 else 0.03

    if signal_type == "MOMENTUM":
        # High conviction — trail the stop, no hard target cap
        trailing_stop_pct = max(0.10, min(0.15, atr_pct * 3))
        stop_loss_pct = atr_pct * 2.0
        invalidation = f"Close below entry - {stop_loss_pct:.0%} = pattern failed"
        return ExitPlan(
            hard_target_pct=None,              # no cap — let it run
            trailing_stop_pct=round(trailing_stop_pct, 3),
            stop_loss_pct=round(stop_loss_pct, 3),
            invalidation_note=invalidation,
        )
    else:
        # Standard swing — hard target based on pattern
        target_map = {
            "bull_flag":           0.15,
            "breakout":            0.18,
            "falling_wedge":       0.20,
            "ascending_triangle":  0.17,
            "cup_and_handle":      0.25,   # cup & handle targets are larger
            "double_bottom":       0.18,
            "base_building":       0.15,
        }
        target = target_map.get(pattern, 0.18)
        # Scale target slightly by confidence
        target = round(target * (0.8 + confidence * 0.4), 3)
        stop_loss_pct = max(0.05, min(0.10, atr_pct * 1.5))
        invalidation = f"Close below MA50 or -{stop_loss_pct:.0%} from entry = exit"
        return ExitPlan(
            hard_target_pct=target,
            trailing_stop_pct=None,
            stop_loss_pct=round(stop_loss_pct, 3),
            invalidation_note=invalidation,
        )


def run(
    candidate: ScreenCandidate,
    hunter_score: Optional[float] = None,
    atr: Optional[float] = None,
    force_sonnet: bool = False,
) -> SwingSignal:
    """
    Main entry: take a ScreenCandidate → return SwingSignal.

    Args:
        candidate:     output from core/screener.py
        hunter_score:  Hunter numerical score (if already computed). Used for
                       SWING vs MOMENTUM classification and promotion eligibility.
        atr:           20-day ATR for stop sizing (from PriceData)
        force_sonnet:  skip Haiku, go straight to Sonnet (use for promotion re-scans)
    """
    ticker = candidate.ticker

    # 1. Render daily chart
    png_bytes = render_chart(ticker, period="1y", interval="1d")
    if not png_bytes:
        return _null_signal(candidate, "chart_render_failed")

    haiku_used = False
    sonnet_escalated = False
    vision_result: Optional[ChartVisionResult] = None

    if force_sonnet:
        # Promotion re-scan or override — use full Sonnet vision
        vision_result = vision_cache.get_or_fetch(
            ticker=ticker, timeframe="daily",
            period="1y", interval="1d",
            current_price=candidate.price,
        )
        sonnet_escalated = True
    else:
        # 2. Haiku first-pass
        haiku_used = True
        pattern, confidence, reason = _haiku_first_pass(ticker, png_bytes)

        logger.info(
            f"[SwingScreener] {ticker} Haiku: pattern={pattern} "
            f"confidence={confidence:.0%} — {reason}"
        )

        if pattern == "none" or confidence < 0.50:
            # No pattern found — still return a signal for WATCH tracking
            return _null_signal(candidate, f"haiku_no_pattern:{pattern}:{confidence:.2f}")

        if confidence >= 0.60:
            # Escalate to Sonnet for full structured analysis
            sonnet_escalated = True
            vision_result = vision_cache.get_or_fetch(
                ticker=ticker, timeframe="daily",
                period="1y", interval="1d",
                current_price=candidate.price,
            )
        else:
            # Haiku found something weak — build minimal signal without Sonnet
            vision_result = ChartVisionResult(
                ticker=ticker,
                pattern=pattern,
                pattern_confidence=confidence,
                ma_crossover_type="none",
                ma_crossover_recency="none",
                ma_crossover_quality="n/a",
                price_structure="BASING",
                support_levels=[],
                resistance_levels=[],
                pattern_thesis=reason,
                actionable_lean="WATCH",
                chart_score_delta=0.0,
            )

    if vision_result is None or vision_result.error:
        return _null_signal(candidate, f"vision_failed:{getattr(vision_result,'error','unknown')}")

    # 3. Classify signal type
    #    MOMENTUM if: hunter_score 7+ AND pattern confidence high AND structure advancing
    is_momentum = (
        hunter_score is not None
        and hunter_score >= 7.0
        and vision_result.pattern_confidence >= 0.65
        and vision_result.price_structure in ("ADVANCING", "BASING")
        and vision_result.actionable_lean == "BUY"
    )
    signal_type = "MOMENTUM" if is_momentum else "SWING"

    # 4. Promotion eligibility
    #    Can be promoted to LONG-TERM if Hunter 8+ at re-scan point
    promotion_eligible = (
        hunter_score is not None
        and hunter_score >= 7.5
        and vision_result.pattern in ("cup_and_handle", "ascending_triangle",
                                       "double_bottom", "breakout", "bull_flag")
        and vision_result.price_structure == "ADVANCING"
    )

    # 5. Build exit plan
    exit_plan = _build_exit_plan(
        entry_price=candidate.price,
        atr=atr,
        pattern=vision_result.pattern,
        signal_type=signal_type,
        confidence=vision_result.pattern_confidence,
    )

    signal = SwingSignal(
        ticker=ticker,
        sector=candidate.sector,
        signal_type=signal_type,
        screens_matched=candidate.screens_matched,
        pattern=vision_result.pattern,
        pattern_confidence=vision_result.pattern_confidence,
        price_structure=vision_result.price_structure,
        ma_crossover_type=vision_result.ma_crossover_type,
        entry_price=candidate.price,
        exit_plan=exit_plan,
        pattern_thesis=vision_result.pattern_thesis,
        actionable_lean=vision_result.actionable_lean,
        chart_score_delta=vision_result.chart_score_delta,
        haiku_used=haiku_used,
        sonnet_escalated=sonnet_escalated,
        promotion_eligible=promotion_eligible,
    )

    logger.info(
        f"[SwingScreener] {ticker} → {signal_type} | "
        f"pattern={signal.pattern} ({signal.pattern_confidence:.0%}) | "
        f"structure={signal.price_structure} | "
        f"lean={signal.actionable_lean} | "
        f"target={exit_plan.hard_target_pct or 'trail'} | "
        f"stop=-{exit_plan.stop_loss_pct:.0%} | "
        f"promote_eligible={promotion_eligible} | "
        f"haiku={'yes' if haiku_used else 'no'} escalated={'yes' if sonnet_escalated else 'no'}"
    )
    return signal


def run_batch(
    candidates: list[ScreenCandidate],
    hunter_scores: Optional[dict[str, float]] = None,
    atrs: Optional[dict[str, float]] = None,
) -> list[SwingSignal]:
    """
    Run swing screening on a list of ScreenCandidates (parallel).
    Returns only actionable signals (WATCH or BUY lean, no errors).

    Args:
        candidates:    list from core/screener.swing_candidates()
        hunter_scores: {ticker: score} if Hunter already ran (optional)
        atrs:          {ticker: atr_20d} for stop sizing (optional)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    hunter_scores = hunter_scores or {}
    atrs = atrs or {}

    def _run_one(c: ScreenCandidate) -> Optional[SwingSignal]:
        try:
            sig = run(
                c,
                hunter_score=hunter_scores.get(c.ticker),
                atr=atrs.get(c.ticker),
            )
            return sig if sig.actionable_lean in ("BUY", "WATCH") else None
        except Exception as e:
            logger.error(f"[SwingScreener] {c.ticker} failed: {e}")
            return None

    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:   # 4 concurrent vision calls
        futures = {pool.submit(_run_one, c): c for c in candidates}
        for future in as_completed(futures):
            result = future.result()
            if result and result.pattern != "none":
                results.append(result)

    # Sort: MOMENTUM first, then by pattern confidence
    results.sort(key=lambda s: (
        0 if s.signal_type == "MOMENTUM" else 1,
        -s.pattern_confidence,
    ))

    momentum = [s for s in results if s.signal_type == "MOMENTUM"]
    swings = [s for s in results if s.signal_type == "SWING"]
    promotable = [s for s in results if s.promotion_eligible]

    logger.info(
        f"[SwingScreener] Batch complete: {len(results)} signals — "
        f"{len(momentum)} MOMENTUM, {len(swings)} SWING, "
        f"{len(promotable)} promotion-eligible"
    )
    return results


def check_promotion(
    ticker: str,
    current_price: float,
    entry_price: float,
    original_signal: SwingSignal,
    hunter_score: float,
    atr: Optional[float] = None,
) -> dict:
    """
    Called when a swing position hits its initial target.
    Re-scans with Sonnet to decide: take profit or promote to MOMENTUM/LONG-TERM.

    Returns dict with:
      action:   "EXIT" | "PROMOTE_MOMENTUM" | "PROMOTE_LONG_TERM"
      reason:   explanation string
      new_exit: updated ExitPlan if promoted
    """
    gain_pct = (current_price - entry_price) / entry_price

    # Re-scan chart with Sonnet (force fresh — ignore cache)
    from core import vision_cache as vc
    vc.invalidate(ticker, "daily")

    candidate = ScreenCandidate(
        ticker=ticker,
        sector=original_signal.sector,
        screens_matched=original_signal.screens_matched,
        price=current_price,
        rsi=0.0,   # not used in promotion
        ma50=0.0,
        ma200=0.0,
        week_52_high_pct=0.0,
        stage="?",
        signal_type=original_signal.signal_type,
        notes="promotion re-scan",
    )

    new_signal = run(candidate, hunter_score=hunter_score, atr=atr, force_sonnet=True)

    # Decision logic
    if new_signal.actionable_lean == "AVOID" or new_signal.price_structure in ("TOPPING", "DISTRIBUTION"):
        return {
            "action": "EXIT",
            "reason": f"Structure turned {new_signal.price_structure} at +{gain_pct:.0%} — take profit",
            "new_exit": None,
        }

    if hunter_score >= 8.0 and new_signal.price_structure == "ADVANCING" and new_signal.pattern_confidence >= 0.65:
        new_exit = _build_exit_plan(current_price, atr, new_signal.pattern, "MOMENTUM", new_signal.pattern_confidence)
        return {
            "action": "PROMOTE_LONG_TERM",
            "reason": (
                f"Hunter {hunter_score:.1f} + {new_signal.price_structure} structure + "
                f"{new_signal.pattern} ({new_signal.pattern_confidence:.0%}) — "
                f"promote to long-term, switch to DCA"
            ),
            "new_exit": new_exit,
        }

    if hunter_score >= 7.0 and new_signal.price_structure == "ADVANCING":
        new_exit = _build_exit_plan(current_price, atr, new_signal.pattern, "MOMENTUM", new_signal.pattern_confidence)
        return {
            "action": "PROMOTE_MOMENTUM",
            "reason": (
                f"Hunter {hunter_score:.1f} + {new_signal.price_structure} structure — "
                f"promote to MOMENTUM, trail stop {new_exit.trailing_stop_pct:.0%}"
            ),
            "new_exit": new_exit,
        }

    return {
        "action": "EXIT",
        "reason": f"Target hit +{gain_pct:.0%}, thesis not strong enough to hold — take profit",
        "new_exit": None,
    }
