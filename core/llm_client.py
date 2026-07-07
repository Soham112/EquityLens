"""
LLM Client — centralized Anthropic API access with model routing.

Model tiers:
  HAIKU  — fast, cheap: mechanical tasks, data formatting
  SONNET — balanced: validator decisions, morning briefings
  OPUS   — most capable: weekly review, pattern synthesis, complex reasoning

Optimizations:
  - Prompt caching: system prompts are cached after first call (10% cost on repeats)
  - Rate limit throttle: max 5 concurrent LLM calls via semaphore
  - Smart model routing: WATCH/PASS signals use Haiku, BUY signals use Sonnet
"""
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Model IDs
HAIKU  = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
OPUS   = "claude-opus-4-8"

_client = None

# Throttle: max 5 concurrent LLM calls to avoid rate limit spikes
_semaphore = threading.Semaphore(5)


def get_client():
    global _client
    if _client is None:
        import anthropic
        from pathlib import Path
        from dotenv import load_dotenv
        # Load .env from project root (two levels up from core/)
        load_dotenv(Path(__file__).parent.parent / ".env")
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set — LLM enrichment disabled, using formula fallback.")
            return None
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def call_llm(
    system: str,
    user: str,
    model: str = SONNET,
    max_tokens: int = 1024,
    retries: int = 3,
    temperature: float = 0.1,  # kept for signature compat; not sent to API (deprecated on Opus 4.x+)
) -> Optional[str]:
    """
    Single LLM call with prompt caching and concurrency throttling.
    - System prompt is marked for caching — repeated calls in same scan pay ~10% cost
    - Max 5 concurrent calls enforced via semaphore
    - Exponential backoff on rate limit errors
    Returns response text or None on failure.
    """
    client = get_client()
    if client is None:
        return None

    with _semaphore:  # throttle concurrent calls
        for attempt in range(retries):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    # Cache the system prompt — identical across all ticker calls in a scan
                    system=[{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user}],
                )
                return response.content[0].text
            except Exception as e:
                err = str(e).lower()
                if "rate_limit" in err or "429" in err:
                    # Rate limit hit — back off longer
                    wait = (2 ** attempt) * 3
                    logger.warning(f"Rate limit hit (attempt {attempt+1}/{retries}) — backing off {wait}s")
                    time.sleep(wait)
                elif attempt < retries - 1:
                    wait = 2 ** attempt
                    logger.warning(f"LLM call failed (attempt {attempt+1}/{retries}): {e} — retrying in {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"LLM call failed after {retries} attempts: {e}")
                    return None
