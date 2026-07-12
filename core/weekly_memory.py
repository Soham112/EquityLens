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


# ══════════════════════════════════════════════════════════════════════════════
# Watch-item scoring — the self-accountability layer.
#
# Each week we mechanically DERIVE the concrete cautions the review is making
# (a position at risk, a leading sector that's decelerating), store them, and the
# NEXT week we SCORE whether each one materialised. Derivation is rule-based (not
# LLM-parsed) so it's deterministic, testable, and grounded in the same structured
# facts the narrative sees — no prose parsing, no anchoring.
#
# A watch-item is a directional claim with a checkable metric:
#   position / "bearish" → we expect further weakness (return keeps falling / stop)
#   sector   / "cooling" → we expect a LEADING+decelerating sector to weaken
# ══════════════════════════════════════════════════════════════════════════════

_AT_RISK_RETURN = -0.08     # a position down ≥8% is "worth watching"


def derive_watch_items(review_date: str, positions: list[dict], sectors: list[dict]) -> list[dict]:
    """Mechanically extract this week's checkable cautions from the review facts."""
    items: list[dict] = []
    for p in positions or []:
        t = p.get("ticker")
        if not t:
            continue
        ret = p.get("return_pct")
        status = p.get("stop_status", "NORMAL")
        alerts = p.get("alerts") or []
        at_risk = (status not in (None, "NORMAL")) or (ret is not None and ret <= _AT_RISK_RETURN) or bool(alerts)
        if at_risk:
            items.append({
                "id": f"pos:{t}", "kind": "position", "subject": t,
                "direction": "bearish", "metric": "return_pct",
                "baseline": round(ret, 4) if ret is not None else None,
                "reason": (alerts[0] if alerts else f"{_pct(ret)}, stop {status}"),
                "created": review_date,
            })
    for s in sectors or []:
        name = s.get("sector")
        notes = (s.get("notes") or "").lower()
        if name and s.get("rotation_rank") == "LEADING" and "decelerat" in notes:
            items.append({
                "id": f"sec:{name}", "kind": "sector", "subject": name,
                "direction": "cooling", "metric": "return_60d",
                "baseline": s.get("return_60d"), "rank_baseline": "LEADING",
                "reason": f"LEADING but decelerating (60d {_pct(s.get('return_60d'))})",
                "created": review_date,
            })
    return items


def _find_closed_pnl(trade_stats: dict, ticker: str):
    """Realized P&L for a ticker from this week's trade_stats, or None if not found."""
    if not trade_stats:
        return None
    for track in ("long_term", "swing"):
        for tr in (trade_stats.get(track) or {}).get("trades", []) or []:
            if tr.get("ticker") == ticker and tr.get("realized_pnl") is not None:
                return tr["realized_pnl"]
    return None


def score_prior_watch_items(before_date: str, positions: list[dict],
                            sectors: list[dict], trade_stats: dict | None = None) -> dict | None:
    """Score the most recent prior review's watch-items against THIS week's facts.
    Returns None if there is no prior review at all."""
    priors = load_recent_reviews(before_date, n=1)
    if not priors:
        return None
    prev = priors[0]
    prior_items = prev.get("watch_items") or []
    base = {"scored_from": prev.get("review_date"), "items": [],
            "resolved": 0, "hits": 0, "unresolved": 0, "hit_rate": None}
    if not prior_items:
        base["note"] = "prior review stored no watch-items (feature was newer) — scoring starts next week"
        return base

    pos_by = {p.get("ticker"): p for p in (positions or [])}
    sec_by = {s.get("sector"): s for s in (sectors or [])}
    scored, hits, resolved, unresolved = [], 0, 0, 0

    for it in prior_items:
        subj, kind, b = it.get("subject"), it.get("kind"), it.get("baseline")
        outcome, current, detail = "UNRESOLVED", None, ""

        if kind == "position":
            cur = pos_by.get(subj)
            if cur is not None:
                current = cur.get("return_pct")
                if current is not None and b is not None:
                    if current < b - 1e-9:      # bearish call: expected further decline
                        outcome, detail = "HIT", f"{_pct(b)} → {_pct(current)} (fell further)"
                    else:
                        outcome, detail = "MISS", f"{_pct(b)} → {_pct(current)} (held / recovered)"
            else:                               # exited since last week
                pnl = _find_closed_pnl(trade_stats, subj)
                if pnl is not None:
                    current = pnl
                    outcome = "HIT" if pnl < 0 else "MISS"
                    detail = f"closed at a {'loss' if pnl < 0 else 'gain'} (${pnl:.0f})"
                else:
                    detail = "no longer held; outcome unclassified"

        elif kind == "sector":
            cur = sec_by.get(subj)
            if cur is not None:
                current, rank = cur.get("return_60d"), cur.get("rotation_rank")
                cooled = (current is not None and b is not None and current < b) or (rank != "LEADING")
                outcome = "HIT" if cooled else "MISS"
                detail = (f"60d {_pct(b)} → {_pct(current)}, rank {rank}"
                          if cooled else f"still hot: 60d {_pct(current)}, {rank}")
            else:
                detail = "sector missing from this week's snapshot"

        if outcome == "HIT":
            hits += 1; resolved += 1
        elif outcome == "MISS":
            resolved += 1
        else:
            unresolved += 1
        scored.append({
            "subject": subj, "kind": kind, "direction": it.get("direction"),
            "reason": it.get("reason"), "baseline": b,
            "current": current, "outcome": outcome, "detail": detail,
        })

    result = {"scored_from": prev.get("review_date"), "items": scored,
              "resolved": resolved, "hits": hits, "unresolved": unresolved,
              "hit_rate": round(hits / resolved, 3) if resolved else None}
    logger.info(f"[WeeklyMemory] watch-item scorecard vs {prev.get('review_date')}: "
                f"{hits}/{resolved} materialised ({unresolved} unresolved)")
    return result


def watch_item_track_record(before_date: str, n: int = 12) -> dict:
    """Running watch-item hit rate over the last n reviews' stored scorecards —
    the platform's foresight track record."""
    H = R = weeks = 0
    for r in load_recent_reviews(before_date, n):
        ws = r.get("watch_scores") or {}
        if ws.get("resolved"):
            weeks += 1
            H += ws.get("hits", 0)
            R += ws.get("resolved", 0)
    return {"reviews_with_scores": weeks, "hits": H, "resolved": R,
            "hit_rate": round(H / R, 3) if R else None}
