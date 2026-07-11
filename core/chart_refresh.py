"""
Chart Vision Refresh — re-runs chart analysis on stale swing candidates.
Marks candidates as STALE if entry_type changed to 'wait' or R/R dropped below 1.5.
"""
import datetime
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = "data"
MIN_RISK_REWARD = 1.5


def _load_swing_candidates(date_str: Optional[str] = None) -> Optional[dict]:
    """Load swing candidates file for today (or specified date)."""
    date_str = date_str or datetime.date.today().isoformat()
    path = os.path.join(DATA_DIR, f"swing_candidates_{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[ChartRefresh] Could not load {path}: {e}")
        return None


def _save_swing_candidates(data: dict, date_str: Optional[str] = None) -> None:
    date_str = date_str or datetime.date.today().isoformat()
    path = os.path.join(DATA_DIR, f"swing_candidates_{date_str}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def refresh_stale_candidates(max_age_days: int = 3) -> list[dict]:
    """
    Read swing_candidates_{today}.json.
    For each candidate where analyzed_at is > max_age_days old:
      - Re-run analyze_swing_candidate(ticker)
      - If entry_type changed to 'wait' OR risk_reward dropped below 1.5: mark as STALE
      - If pattern changed: update with new analysis
      - Save updated swing_candidates file
    Returns list of changes made.
    """
    today = datetime.date.today().isoformat()
    data = _load_swing_candidates(today)

    if not data:
        logger.debug("[ChartRefresh] No swing candidates file found for today")
        return []

    candidates = data.get("candidates", [])
    if not candidates:
        return []

    changes = []
    cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age_days)

    try:
        from core.swing_chart_analysis import analyze_swing_candidate
    except ImportError:
        logger.warning("[ChartRefresh] swing_chart_analysis not available")
        return []

    updated_candidates = []
    for candidate in candidates:
        analyzed_at = candidate.get("analyzed_at")
        ticker = candidate.get("ticker", "")

        # Cost cap (user decision 2026-07-10): only 3+/7 candidates earn vision
        # refreshes — the old no-timestamp path re-analyzed the whole 2/7 crowd
        # daily (~15-90 paid calls/day on stocks the scan itself didn't chart).
        # Entry safety is unaffected: auto-entry triggers off the SAVED zones and
        # stops numerically every day; vision only re-checks structure.
        if candidate.get("signals_score", 0) < 3:
            updated_candidates.append(candidate)
            continue

        # Check if stale (charts stay valid for max_age_days; no daily re-looks)
        is_stale = False
        if analyzed_at:
            try:
                analyzed_dt = datetime.datetime.fromisoformat(analyzed_at)
                is_stale = analyzed_dt < cutoff
            except (ValueError, TypeError):
                is_stale = True  # unknown format = treat as stale
        else:
            is_stale = True  # 3+/7 with no chart yet (e.g. vision failed at scan)

        if not is_stale:
            updated_candidates.append(candidate)
            continue

        # Re-analyze
        logger.info(f"[ChartRefresh] Re-analyzing stale candidate: {ticker}")
        try:
            new_signal = analyze_swing_candidate(ticker)
            if new_signal is None:
                updated_candidates.append(candidate)
                continue

            change = {"ticker": ticker, "action": None, "old": {}, "new": {}}

            old_entry_type = candidate.get("entry_type")
            old_rr = candidate.get("risk_reward")
            new_entry_type = new_signal.entry_type
            new_rr = new_signal.risk_reward

            # Mark as STALE if entry changed to 'wait' or R/R degraded
            if new_entry_type == "wait":
                candidate["stale"] = True
                candidate["stale_reason"] = f"Entry type changed to 'wait' (was: {old_entry_type})"
                change["action"] = "MARKED_STALE"
                change["old"] = {"entry_type": old_entry_type, "risk_reward": old_rr}
                change["new"] = {"entry_type": new_entry_type, "risk_reward": new_rr}
                changes.append(change)
            elif new_rr is not None and new_rr < MIN_RISK_REWARD:
                candidate["stale"] = True
                candidate["stale_reason"] = f"Risk/reward dropped to {new_rr:.1f}x (min {MIN_RISK_REWARD}x)"
                change["action"] = "MARKED_STALE"
                change["old"] = {"entry_type": old_entry_type, "risk_reward": old_rr}
                change["new"] = {"entry_type": new_entry_type, "risk_reward": new_rr}
                changes.append(change)
            else:
                # Update with fresh analysis
                old_pattern = candidate.get("pattern")
                candidate.update({
                    "entry_type": new_signal.entry_type,
                    "pattern": new_signal.pattern,
                    "pattern_confidence": new_signal.pattern_confidence,
                    "entry_zone_low": new_signal.entry_zone_low,
                    "entry_zone_high": new_signal.entry_zone_high,
                    "stop_level": new_signal.stop_level,
                    "target_level": new_signal.target_level,
                    "risk_reward": new_signal.risk_reward,
                    "chart_thesis": new_signal.chart_thesis,
                    "chart_path": new_signal.chart_path,
                    "analyzed_at": new_signal.analyzed_at,
                    "stale": False,
                })
                if old_pattern != new_signal.pattern:
                    change["action"] = "PATTERN_CHANGED"
                    change["old"] = {"pattern": old_pattern, "entry_type": old_entry_type}
                    change["new"] = {"pattern": new_signal.pattern, "entry_type": new_entry_type}
                    changes.append(change)
                else:
                    change["action"] = "REFRESHED"
                    changes.append(change)

        except Exception as e:
            logger.warning(f"[ChartRefresh] Could not re-analyze {ticker}: {e}")

        updated_candidates.append(candidate)

    if changes:
        data["candidates"] = updated_candidates
        data["last_chart_refresh"] = datetime.datetime.now().isoformat()
        _save_swing_candidates(data, today)
        logger.info(f"[ChartRefresh] Applied {len(changes)} chart refresh changes")

    return changes


def check_longterm_chart_freshness(result) -> bool:
    """
    Returns True if the BUY signal's chart was analyzed more than 3 days ago (needs refresh).
    result: AnalysisResult with optional lt_chart field
    """
    lt_chart = getattr(result, "lt_chart", None)
    if lt_chart is None:
        return False  # no chart analysis — nothing to refresh

    analyzed_at = getattr(lt_chart, "analyzed_at", None)
    if not analyzed_at:
        return True  # no timestamp = stale

    try:
        analyzed_dt = datetime.datetime.fromisoformat(analyzed_at)
        age_days = (datetime.datetime.now() - analyzed_dt).days
        return age_days > 3
    except (ValueError, TypeError):
        return True
