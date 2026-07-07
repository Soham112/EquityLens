"""
Chart Vision Agent — sends a rendered stock chart to Claude's vision API
and extracts structured pattern analysis.

Unique value over Hunter's numerical signals:
  - Chart pattern detection (cup & handle, H&S, wedge, flag, etc.)
  - MA crossover recency and quality (not just MA50 > MA200 boolean)
  - Overall price structure stage (basing / advancing / topping / distribution)

Does NOT re-analyze RSI zone or volume numerics — Hunter already has those.
"""
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from core.chart_renderer import render_chart
from core.llm_client import SONNET, get_client

logger = logging.getLogger(__name__)

# Patterns the model must choose from — constrained list prevents hallucination of exotic patterns
KNOWN_PATTERNS = [
    "cup_and_handle",
    "inverse_cup_and_handle",
    "head_and_shoulders",
    "inverse_head_and_shoulders",
    "double_top",
    "double_bottom",
    "ascending_triangle",
    "descending_triangle",
    "symmetrical_triangle",
    "rising_wedge",
    "falling_wedge",
    "bull_flag",
    "bear_flag",
    "breakout",           # clean break above key resistance
    "breakdown",          # clean break below key support
    "base_building",      # tight sideways consolidation, no clear pattern yet
    "uptrend",            # steady higher highs / higher lows, no specific pattern
    "downtrend",          # steady lower highs / lower lows
    "none",               # chart is too noisy or ambiguous to classify
]

_SYSTEM = """\
You are a professional technical analyst with 20 years of experience reading equity charts.
You will receive a weekly candlestick chart with MA50, MA200, volume, and RSI panels.

Your job is to identify:
1. The dominant chart pattern (pick exactly ONE from the provided list)
2. The MA crossover situation — not just whether MA50 > MA200, but HOW RECENTLY the cross occurred and whether it looks clean or like a potential fake-out
3. The price structure stage: BASING | ADVANCING | TOPPING | DISTRIBUTION | RECOVERY
4. Key support and resistance price levels visible on the chart (up to 3 each)
5. A 1-2 sentence pattern thesis — what this chart is saying about near-term direction

Rules:
- Be specific and direct. No hedging language.
- If the pattern is ambiguous, choose "none" — do not force a pattern.
- Confidence must reflect genuine certainty, not optimism.
- Output ONLY valid JSON matching the schema below. No extra text.
"""

_USER_TEMPLATE = """\
Ticker: {ticker}

Allowed patterns (pick exactly one): {patterns}

Respond with this exact JSON schema:
{{
  "pattern": "<one value from allowed patterns>",
  "pattern_confidence": <0.0-1.0>,
  "ma_crossover": {{
    "type": "golden_cross" | "death_cross" | "none",
    "recency": "recent" | "established" | "aging" | "none",
    "quality": "clean" | "fake_out_risk" | "n/a"
  }},
  "price_structure": "BASING" | "ADVANCING" | "TOPPING" | "DISTRIBUTION" | "RECOVERY",
  "support_levels": [<price>, ...],
  "resistance_levels": [<price>, ...],
  "pattern_thesis": "<1-2 sentences>",
  "actionable_lean": "BUY" | "WATCH" | "AVOID"
}}
"""


@dataclass
class ChartVisionResult:
    ticker: str
    pattern: str
    pattern_confidence: float
    ma_crossover_type: str       # "golden_cross" | "death_cross" | "none"
    ma_crossover_recency: str    # "recent" | "established" | "aging" | "none"
    ma_crossover_quality: str    # "clean" | "fake_out_risk" | "n/a"
    price_structure: str         # "BASING" | "ADVANCING" | "TOPPING" | "DISTRIBUTION" | "RECOVERY"
    support_levels: list[float]
    resistance_levels: list[float]
    pattern_thesis: str
    actionable_lean: str         # "BUY" | "WATCH" | "AVOID"
    chart_score_delta: float     # adjustment applied to Hunter technical score (-1.0 to +1.0)
    error: Optional[str] = None


def _score_delta(result: dict) -> float:
    """
    Translate vision findings into a small Hunter technical score adjustment.
    Range: -1.0 to +1.0. Kept conservative — vision confirms, not overrides.
    """
    delta = 0.0
    pattern = result.get("pattern", "none")
    confidence = result.get("pattern_confidence", 0.0)
    lean = result.get("actionable_lean", "WATCH")
    structure = result.get("price_structure", "")
    crossover = result.get("ma_crossover", {})

    # Bullish patterns
    if pattern in ("cup_and_handle", "inverse_head_and_shoulders", "double_bottom",
                   "ascending_triangle", "falling_wedge", "bull_flag", "breakout"):
        delta += 0.6 * confidence
    # Bearish patterns
    elif pattern in ("head_and_shoulders", "inverse_cup_and_handle", "double_top",
                     "descending_triangle", "rising_wedge", "bear_flag", "breakdown"):
        delta -= 0.6 * confidence

    # Structure bonus
    if structure == "ADVANCING":
        delta += 0.2
    elif structure in ("TOPPING", "DISTRIBUTION"):
        delta -= 0.2

    # MA crossover bonus
    cross = crossover.get("type", "none")
    recency = crossover.get("recency", "none")
    quality = crossover.get("quality", "n/a")
    if cross == "golden_cross" and recency in ("recent", "established") and quality == "clean":
        delta += 0.2
    elif cross == "death_cross" and recency in ("recent", "established"):
        delta -= 0.2

    # Lean override — if vision says AVOID, cap delta at 0
    if lean == "AVOID":
        delta = min(delta, 0.0)
    elif lean == "BUY":
        delta = max(delta, 0.0)

    return round(max(-1.0, min(1.0, delta)), 2)


def run(ticker: str, period: str = "5y", interval: str = "1wk") -> ChartVisionResult:
    """
    Main entry: render chart → send to Claude vision → return structured result.
    Falls back to a neutral no-op result on any failure so orchestrator never crashes.
    """
    _null = ChartVisionResult(
        ticker=ticker, pattern="none", pattern_confidence=0.0,
        ma_crossover_type="none", ma_crossover_recency="none", ma_crossover_quality="n/a",
        price_structure="BASING", support_levels=[], resistance_levels=[],
        pattern_thesis="Chart analysis unavailable.", actionable_lean="WATCH",
        chart_score_delta=0.0,
    )

    # 1. Render chart
    png_bytes = render_chart(ticker, period=period, interval=interval)
    if not png_bytes:
        _null.error = "chart_render_failed"
        return _null

    # 2. Encode image
    b64 = base64.standard_b64encode(png_bytes).decode("utf-8")

    # 3. Call Claude vision
    client = get_client()
    if client is None:
        _null.error = "no_api_key"
        return _null

    user_text = _USER_TEMPLATE.format(
        ticker=ticker,
        patterns=", ".join(KNOWN_PATTERNS),
    )

    try:
        response = client.messages.create(
            model=SONNET,
            max_tokens=600,
            temperature=0.1,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }],
        )
        raw_text = response.content[0].text.strip()
    except Exception as e:
        logger.error(f"[ChartVision] API call failed for {ticker}: {e}")
        _null.error = str(e)
        return _null

    # 4. Parse JSON response
    try:
        # Strip markdown fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"[ChartVision] JSON parse failed for {ticker}: {e}\nRaw: {raw_text[:300]}")
        _null.error = "json_parse_failed"
        return _null

    crossover = data.get("ma_crossover", {})
    delta = _score_delta(data)

    result = ChartVisionResult(
        ticker=ticker,
        pattern=data.get("pattern", "none"),
        pattern_confidence=float(data.get("pattern_confidence", 0.0)),
        ma_crossover_type=crossover.get("type", "none"),
        ma_crossover_recency=crossover.get("recency", "none"),
        ma_crossover_quality=crossover.get("quality", "n/a"),
        price_structure=data.get("price_structure", "BASING"),
        support_levels=[float(x) for x in data.get("support_levels", [])],
        resistance_levels=[float(x) for x in data.get("resistance_levels", [])],
        pattern_thesis=data.get("pattern_thesis", ""),
        actionable_lean=data.get("actionable_lean", "WATCH"),
        chart_score_delta=delta,
    )

    logger.info(
        f"[ChartVision] {ticker} | pattern={result.pattern} ({result.pattern_confidence:.0%}) | "
        f"structure={result.price_structure} | crossover={result.ma_crossover_type}/{result.ma_crossover_recency} | "
        f"lean={result.actionable_lean} | delta={result.chart_score_delta:+.2f}"
    )
    return result
