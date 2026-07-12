"""
Weekly memory — lean, fact-based continuity for the Sunday outcome review.

Each weekly review is saved to data/weekly_review_{date}.json but, until now,
nothing read those files back — every week's narrative started from a blank slate.
This helper loads the last few reviews and distills them into a COMPACT, mostly
FACTUAL digest that gets fed to the narrative generator so it can:

  1. speak to trajectory ("MRNA -8% last week, -13% now — deteriorating"),
  2. notice sector-regime shifts across weeks, and
  3. check whether last week's cautions actually played out (self-accountability,
     the EXPERIMENTS.md discipline applied to market observations).

Design choices (deliberately lean — see the weekly-memory discussion):
  - Feed FACTS (sector ranks, per-position return trajectories) rather than only
    prose, so the model reasons from data instead of anchoring on its own old take.
  - Include the prior week's narrative ONLY as a short, clearly-labelled "take to
    verify," never as ground truth.
  - Rolling window of N weeks (default 3) to keep the prompt small.
  - Pure read-only; a failure here must never break the review (callers guard it).

Future (not built yet, would be its own change): emit structured watch-items each
week and auto-score whether they materialised, instead of leaving that to the LLM.
"""
import glob
import json
import logging
import os

logger = logging.getLogger(__name__)

DATA_DIR = "data"
_SUMMARY_MAX = 320          # chars of the prior narrative to carry forward
_TOP_SECTORS = 6            # cap the sector trajectory to the strongest few


def load_recent_reviews(before_date: str, n: int = 3) -> list[dict]:
    """The n most recent weekly_review_*.json files with a date strictly before
    `before_date`, newest first. Never raises — returns [] on any trouble."""
    out: list[dict] = []
    try:
        paths = sorted(glob.glob(os.path.join(DATA_DIR, "weekly_review_*.json")), reverse=True)
        for p in paths:
            try:
                d = json.load(open(p))
            except Exception:
                continue
            rdate = d.get("review_date") or ""
            if rdate and rdate < before_date:
                out.append(d)
            if len(out) >= n:
                break
    except Exception as e:
        logger.debug(f"weekly_memory.load_recent_reviews: {e}")
    return out


def _pct(x) -> str:
    try:
        return f"{float(x):+.1%}"
    except (TypeError, ValueError):
        return "n/a"


def build_context_digest(before_date: str, n: int = 3) -> dict | None:
    """
    Compact, mostly-factual continuity digest of the last n reviews (before
    `before_date`). Returns None when there is no prior history to draw on.

    Shape:
      {
        "weeks_covered": ["2026-07-05", "2026-06-21"],
        "sector_regime_trajectory": {           # strongest sectors, oldest→newest
            "technology": ["2026-06-21 LEADING +18.9%", "2026-07-05 LEADING +23.8%"],
            ...
        },
        "position_return_trajectory": {         # names seen in >1 prior week
            "MRNA": ["2026-06-21 -4.1%", "2026-07-05 -8.2%"],
            ...
        },
        "last_week_take": "…truncated prior narrative to verify against this week…",
      }
    """
    reviews = load_recent_reviews(before_date, n)
    if not reviews:
        return None

    chron = list(reversed(reviews))  # oldest → newest for readable trajectories

    # ── sector regime trajectory (cap to the strongest sectors most recently) ──
    latest_secs = reviews[0].get("sector_snapshot") or []
    keep = {s.get("sector") for s in latest_secs[:_TOP_SECTORS] if s.get("sector")}
    sector_traj: dict[str, list[str]] = {}
    for r in chron:
        for s in (r.get("sector_snapshot") or []):
            name = s.get("sector")
            if name not in keep:
                continue
            sector_traj.setdefault(name, []).append(
                f"{r.get('review_date')} {s.get('rotation_rank', '?')} {_pct(s.get('return_60d'))}"
            )

    # ── per-position return trajectory ──
    # Keep any name still held last week (so the model always has a week-over-week
    # comparison against THIS week's open_positions in the payload) plus any name
    # with multi-week history. Drops only names that churned out weeks ago.
    last_week_names = {p.get("ticker") for p in (reviews[0].get("open_positions") or [])}
    pos_seen: dict[str, list[str]] = {}
    for r in chron:
        for p in (r.get("open_positions") or []):
            t = p.get("ticker")
            if not t:
                continue
            pos_seen.setdefault(t, []).append(f"{r.get('review_date')} {_pct(p.get('return_pct'))}")
    pos_traj = {t: v for t, v in pos_seen.items() if t in last_week_names or len(v) > 1}

    # ── last week's narrative, trimmed, as a "take to verify" (not ground truth) ──
    last_take = (reviews[0].get("summary") or "").strip().replace("\n", " ")
    if len(last_take) > _SUMMARY_MAX:
        last_take = last_take[:_SUMMARY_MAX].rsplit(" ", 1)[0] + "…"

    digest = {
        "weeks_covered": [r.get("review_date") for r in reviews],
        "sector_regime_trajectory": sector_traj,
        "position_return_trajectory": pos_traj,
        "last_week_take": last_take,
    }
    logger.info(
        f"[WeeklyMemory] digest from {len(reviews)} prior week(s): "
        f"{len(sector_traj)} sectors, {len(pos_traj)} tracked positions"
    )
    return digest
