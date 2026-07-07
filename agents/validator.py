"""
Validator Agent — Combines Hunter + Sentiment − Critic into conviction score.
Applies the three-gate system and produces the final BUY/WATCHLIST/AVOID signal.

LLM enrichment: after mathematical conviction is computed, Sonnet writes a
structured investment thesis. Falls back to formula-based thesis if API fails.
"""
import json
import logging
from dataclasses import dataclass
from typing import Optional

from agents.critic import CriticResult
from agents.hunter import HunterResult
from agents.sentiment import SentimentResult
from core.conviction import ConvictionResult, calculate_conviction
from core.llm_client import SONNET, call_llm
from core.staleness import StalenessResult

logger = logging.getLogger(__name__)

_THESIS_SYSTEM = """\
Analyze a quantitative equity signal and generate a precise, actionable investment thesis.

<role>
Act as a senior equity research analyst. You receive structured agent scores that evaluated a stock \
across fundamentals, technicals, valuation, sentiment, and sector momentum. \
Synthesize these scores into a clear thesis a portfolio manager can act on immediately.
</role>

<reasoning_steps>
Step 1: Identify the 1-2 strongest factors driving the signal — look for high sub-scores, \
        momentum flags (MACD crossover, Stage 2, RS vs SPY), or fundamental acceleration signals.
Step 2: Identify the single biggest risk or weakest point undermining the thesis \
        (red flags, low valuation score, sector headwinds, sentiment retroactive).
Step 3: Write the thesis: verdict + primary driver first, supporting factor second, \
        key risk third. Be direct — no hedging language.
</reasoning_steps>

<output_requirements>
- Length: 2-3 sentences, maximum 70 words total
- Start with the actionable verdict and primary reason — do NOT restate the signal label or conviction number
- Second sentence: the strongest technical or fundamental supporting factor with a specific data point
- Third sentence (if needed): the most important risk to monitor going forward
- Tone: direct and factual — avoid "it is worth noting", "it should be mentioned", or similar filler
</output_requirements>

<example>
<input>
{"ticker": "NVDA", "signal": "BUY", "hunter_score": 8.4,
 "hunter_breakdown": {"fundamentals": 4.2, "technicals": 3.2, "valuation": 1.0,
   "flags": ["Revenue growth 122% YoY — exceptional", "MACD bullish crossover", "Stage 2 uptrend", "RS vs SPY +18%"]},
 "sentiment_boost": 0.8, "red_flags": [], "sector": "Technology — Leading sector, MACD bullish"}
</input>
<output>
NVDA's 122% revenue acceleration, confirmed by a MACD crossover and Stage 2 uptrend structure, \
makes this a high-conviction entry with strong sector tailwinds. Relative strength of +18% vs SPY \
signals institutional accumulation. Monitor valuation premium — the low valuation score (1.0/3.0) \
means the thesis breaks if growth decelerates.
</output>
</example>"""


def enrich_thesis(
    ticker: str,
    signal: str,
    conviction: float,
    hunter_score: float,
    sentiment_boost: float,
    red_flags: list,
    sector_notes: str,
    hunter_breakdown: Optional[dict] = None,
) -> Optional[str]:
    """Call Sonnet to generate a narrative investment thesis. Returns None on failure."""
    payload = {
        "ticker": ticker,
        "signal": signal,
        "conviction": round(conviction, 1),
        "hunter_score": round(hunter_score, 1),
        "sentiment_boost": round(sentiment_boost, 2),
        "red_flags": red_flags,
        "sector": sector_notes[:80],
        "hunter_breakdown": hunter_breakdown or {},
    }
    user_msg = (
        "Generate the investment thesis for this signal.\n\n"
        "<signal_data>\n"
        f"{json.dumps(payload, indent=2)}\n"
        "</signal_data>"
    )
    result = call_llm(system=_THESIS_SYSTEM, user=user_msg, model=SONNET, max_tokens=150)
    return result


@dataclass
class ValidatorResult:
    ticker: str
    hunter: HunterResult
    sentiment: SentimentResult
    critic: CriticResult
    staleness: StalenessResult
    conviction: ConvictionResult

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "signal": self.conviction.signal,
            "conviction": self.conviction.conviction,
            "data_confidence": self.conviction.data_confidence,
            "hunter_score": self.hunter.score,
            "sentiment_boost": self.sentiment.sentiment_boost,
            "red_flags": self.critic.red_flag_labels,
            "kill_switch": self.critic.kill_switch,
            "data_quality": self.staleness.data_quality.value,
            "summary": self.conviction.summary,
        }


def run(
    hunter: HunterResult,
    sentiment: SentimentResult,
    critic: CriticResult,
    staleness: StalenessResult,
    sector_penalty: float = 0.0,
) -> ValidatorResult:
    ticker = hunter.ticker

    if not staleness.should_score:
        conviction = calculate_conviction(
            ticker=ticker,
            hunter_raw=0,
            red_flags=["data_too_stale"],
            sentiment_adjustment=0,
            staleness_penalty=0,
            confidence_penalty=0,
        )
        conviction.signal = "AVOID"
        conviction.summary = staleness.status
        return ValidatorResult(
            ticker=ticker,
            hunter=hunter,
            sentiment=sentiment,
            critic=critic,
            staleness=staleness,
            conviction=conviction,
        )

    conviction = calculate_conviction(
        ticker=ticker,
        hunter_raw=hunter.score,
        red_flags=critic.red_flags,
        sentiment_adjustment=sentiment.sentiment_boost,
        staleness_penalty=staleness.conviction_penalty,
        confidence_penalty=staleness.confidence_penalty,
        sector_penalty=sector_penalty,
        missing_critical_data=False,
        data_source_conflict=False,
        retroactive_sentiment=(sentiment.timing_status == "RETROACTIVE"),
        only_tier2_sources=(sentiment.source_status == "INSUFFICIENT_TIER1"),
    )

    return ValidatorResult(
        ticker=ticker,
        hunter=hunter,
        sentiment=sentiment,
        critic=critic,
        staleness=staleness,
        conviction=conviction,
    )
