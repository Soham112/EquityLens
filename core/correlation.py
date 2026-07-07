"""
Correlation Limits [GAP 12]

Prevents over-concentration in correlated positions. When adding a new BUY,
checks whether the portfolio already holds correlated names that collectively
breach the cluster exposure limit.

Algorithm:
  1. Fetch 60-day rolling daily returns for all tickers (held + candidate).
  2. Build correlation matrix.
  3. Cluster tickers with pairwise correlation > CORR_THRESHOLD (default 0.70).
  4. Sum current exposure to each cluster. If adding the candidate would push
     a cluster over CLUSTER_MAX_PCT (default 30%), cap or block the position.

Also pre-defines known clusters for common watchlist names so the check works
even when there's insufficient price history (new listings, thin data).
"""
import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

CORR_THRESHOLD = 0.70       # pairs above this are "correlated"
CLUSTER_MAX_PCT = 0.30      # max total portfolio % in one cluster
HISTORY_DAYS = 60           # rolling window for return correlation
MIN_OVERLAP_DAYS = 30       # minimum shared trading days to compute correlation


# ── Known sector clusters (pre-seeded; avoids cold-start data issues) ────────

PRESET_CLUSTERS: dict[str, list[str]] = {
    "semis":         ["NVDA", "AMD", "AVGO", "ARM", "QCOM", "INTC", "MU", "AMAT", "LRCX"],
    "ai_infra":      ["NVDA", "SMCI", "VRT", "DELL", "HPE"],
    "cloud_saas":    ["MSFT", "GOOGL", "AMZN", "CRM", "NOW", "SNOW", "MDB"],
    "cybersecurity": ["CRWD", "ZS", "NET", "PANW", "S", "FTNT"],
    "ai_software":   ["PLTR", "AI", "BBAI", "SOUN"],
    "mega_tech":     ["MSFT", "AAPL", "GOOGL", "META", "AMZN", "TSLA"],
}

# Build reverse map: ticker → preset cluster names
_TICKER_TO_PRESET: dict[str, list[str]] = {}
for _cluster, _tickers in PRESET_CLUSTERS.items():
    for _t in _tickers:
        _TICKER_TO_PRESET.setdefault(_t, []).append(_cluster)


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class CorrelationCluster:
    name: str              # e.g. "semis" or "dynamic_0"
    members: list[str]     # all tickers in this cluster
    total_exposure_pct: float   # current portfolio % already in cluster
    is_preset: bool


@dataclass
class CorrelationCheckResult:
    ticker: str
    allowed: bool
    capped_pct: Optional[float]     # None = not capped; float = new max size
    blocking_cluster: Optional[str] # which cluster caused the cap/block
    cluster_exposure_before: float  # cluster % before adding candidate
    cluster_exposure_after: float   # cluster % if candidate is added at full size
    cluster_limit: float
    rationale: str
    alerts: list[str] = field(default_factory=list)


# ── Core functions ────────────────────────────────────────────────────────────

def fetch_returns(tickers: list[str], days: int = HISTORY_DAYS) -> dict[str, list[float]]:
    """
    Returns {ticker: [daily_return, ...]} for the last `days` calendar days.
    Skips tickers where data is unavailable.
    """
    import yfinance as yf
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days + 10)  # buffer for weekends
    result: dict[str, list[float]] = {}

    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(
                start=start.isoformat(), end=end.isoformat()
            )
            if hist.empty or len(hist) < MIN_OVERLAP_DAYS:
                continue
            closes = hist["Close"].values[-days:]
            rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
                    for i in range(1, len(closes))]
            result[ticker] = rets
        except Exception as e:
            logger.debug(f"{ticker} returns fetch error: {e}")

    return result


def compute_correlation_matrix(
    returns: dict[str, list[float]],
) -> dict[tuple[str, str], float]:
    """
    Returns {(ticker_a, ticker_b): pearson_corr} for all pairs.
    Only includes pairs with >= MIN_OVERLAP_DAYS shared observations.
    """
    import math
    tickers = list(returns.keys())
    corr_map: dict[tuple[str, str], float] = {}

    for i, a in enumerate(tickers):
        for b in tickers[i + 1:]:
            ra = returns[a]
            rb = returns[b]
            n = min(len(ra), len(rb))
            if n < MIN_OVERLAP_DAYS:
                continue
            ra_s, rb_s = ra[-n:], rb[-n:]
            mean_a = sum(ra_s) / n
            mean_b = sum(rb_s) / n
            cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra_s, rb_s))
            var_a = sum((x - mean_a) ** 2 for x in ra_s)
            var_b = sum((y - mean_b) ** 2 for y in rb_s)
            if var_a == 0 or var_b == 0:
                continue
            r = cov / math.sqrt(var_a * var_b)
            corr_map[(a, b)] = round(r, 3)

    return corr_map


def build_dynamic_clusters(
    corr_map: dict[tuple[str, str], float],
    threshold: float = CORR_THRESHOLD,
) -> list[list[str]]:
    """
    Union-find clustering: group tickers where any pair has corr >= threshold.
    Returns list of clusters (each cluster is a list of tickers).
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent.get(x, x), x)
            x = parent.get(x, x)
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    all_tickers: set[str] = set()
    for (a, b), corr in corr_map.items():
        all_tickers.update([a, b])
        if corr >= threshold:
            union(a, b)

    groups: dict[str, list[str]] = {}
    for t in all_tickers:
        root = find(t)
        groups.setdefault(root, []).append(t)

    return [g for g in groups.values() if len(g) > 1]


def _preset_cluster_exposure(
    candidate: str,
    held_tickers: dict[str, float],  # {ticker: position_pct}
) -> list[tuple[str, float, float]]:
    """
    Returns [(cluster_name, current_exposure_pct, limit)] for all preset clusters
    that contain the candidate ticker.
    """
    cluster_names = _TICKER_TO_PRESET.get(candidate, [])
    results = []
    for name in cluster_names:
        members = set(PRESET_CLUSTERS[name])
        exposure = sum(pct for t, pct in held_tickers.items() if t in members)
        results.append((name, exposure, CLUSTER_MAX_PCT))
    return results


def check_correlation_limit(
    candidate_ticker: str,
    candidate_size_pct: float,     # proposed position size as fraction of portfolio
    held_positions: dict[str, float],  # {ticker: current_position_pct}
    use_dynamic: bool = True,          # also compute dynamic clusters from live returns
) -> CorrelationCheckResult:
    """
    Main entry point. Checks whether adding candidate_ticker at candidate_size_pct
    would breach any correlation cluster limit.

    held_positions: dict mapping currently-held tickers to their portfolio weight (0-1).
    """
    alerts: list[str] = []

    # ── Step 1: Check preset clusters (fast, no data fetch needed) ──
    preset_checks = _preset_cluster_exposure(candidate_ticker, held_positions)
    for cluster_name, current_exp, limit in preset_checks:
        projected = current_exp + candidate_size_pct
        if projected > limit:
            # Cap the position so total cluster stays at limit
            available = max(0.0, limit - current_exp)
            if available < 0.005:
                return CorrelationCheckResult(
                    ticker=candidate_ticker,
                    allowed=False,
                    capped_pct=0.0,
                    blocking_cluster=cluster_name,
                    cluster_exposure_before=current_exp,
                    cluster_exposure_after=projected,
                    cluster_limit=limit,
                    rationale=(
                        f"Cluster '{cluster_name}' already at {current_exp:.0%} "
                        f"(limit {limit:.0%}) — no room for {candidate_ticker}"
                    ),
                    alerts=[
                        f"CORR BLOCK {candidate_ticker}: cluster '{cluster_name}' "
                        f"at {current_exp:.0%} limit {limit:.0%}"
                    ],
                )
            alerts.append(
                f"CORR CAP {candidate_ticker}: cluster '{cluster_name}' "
                f"at {current_exp:.0%} — capping position to {available:.1%}"
            )
            return CorrelationCheckResult(
                ticker=candidate_ticker,
                allowed=True,
                capped_pct=round(available, 3),
                blocking_cluster=cluster_name,
                cluster_exposure_before=current_exp,
                cluster_exposure_after=current_exp + available,
                cluster_limit=limit,
                rationale=(
                    f"Cluster '{cluster_name}' at {current_exp:.0%}; "
                    f"capping {candidate_ticker} to {available:.1%} to stay under {limit:.0%} limit"
                ),
                alerts=alerts,
            )

    # ── Step 2: Dynamic correlation check (live price data) ──
    if use_dynamic and held_positions:
        all_tickers = list(held_positions.keys()) + [candidate_ticker]
        returns = fetch_returns(all_tickers)
        if candidate_ticker in returns and len(returns) > 1:
            corr_map = compute_correlation_matrix(returns)
            clusters = build_dynamic_clusters(corr_map)

            for cluster in clusters:
                if candidate_ticker not in cluster:
                    continue
                current_exp = sum(pct for t, pct in held_positions.items() if t in cluster)
                projected = current_exp + candidate_size_pct
                if projected > CLUSTER_MAX_PCT:
                    available = max(0.0, CLUSTER_MAX_PCT - current_exp)
                    cluster_label = "+".join(sorted(cluster))
                    alerts.append(
                        f"CORR CAP {candidate_ticker}: dynamic cluster [{cluster_label}] "
                        f"at {current_exp:.0%} — capping to {available:.1%}"
                    )
                    return CorrelationCheckResult(
                        ticker=candidate_ticker,
                        allowed=available >= 0.005,
                        capped_pct=round(available, 3) if available >= 0.005 else 0.0,
                        blocking_cluster=cluster_label,
                        cluster_exposure_before=current_exp,
                        cluster_exposure_after=current_exp + (available if available >= 0.005 else 0),
                        cluster_limit=CLUSTER_MAX_PCT,
                        rationale=(
                            f"Dynamic cluster [{cluster_label}] projected at {projected:.0%} "
                            f"(limit {CLUSTER_MAX_PCT:.0%})"
                        ),
                        alerts=alerts,
                    )

    # ── No limits hit ──
    total_exp = sum(pct for t, pct in held_positions.items()
                    if t in _TICKER_TO_PRESET.get(candidate_ticker, []))
    return CorrelationCheckResult(
        ticker=candidate_ticker,
        allowed=True,
        capped_pct=None,
        blocking_cluster=None,
        cluster_exposure_before=0.0,
        cluster_exposure_after=candidate_size_pct,
        cluster_limit=CLUSTER_MAX_PCT,
        rationale=f"No cluster limits breached for {candidate_ticker}",
        alerts=[],
    )


def entry_correlation_check(ticker: str, sector: str, portfolio) -> dict:
    """
    At BUY signal time, compute sector exposure and correlation risk.
    portfolio: PortfolioState from agents/portfolio_manager.py

    Returns: {
        current_sector_pct: float,
        projected_sector_pct: float,
        correlated_tickers: list[str],
        warning: str or None,
        block: bool
    }
    """
    ASSUMED_POSITION_PCT = 0.06  # assume 6% position size
    SECTOR_CAP = 0.30            # 30% per sector hard cap

    # Build held_positions dict from portfolio state
    held_positions: dict[str, float] = {}
    try:
        if hasattr(portfolio, "positions") and portfolio.positions:
            for pos_ticker, pos_data in portfolio.positions.items():
                if isinstance(pos_data, dict):
                    held_positions[pos_ticker] = pos_data.get("position_pct", 0.05)
                else:
                    held_positions[pos_ticker] = 0.05
    except Exception:
        pass

    # Compute current sector exposure
    # Use preset cluster membership as sector proxy
    sector_tickers = []
    for cluster_name, members in PRESET_CLUSTERS.items():
        if sector.lower() in cluster_name.lower() or cluster_name.lower() in sector.lower():
            sector_tickers.extend(members)

    # Also map by sector name directly (approximate)
    SECTOR_CLUSTERS = {
        "semiconductors": ["semis", "ai_infra"],
        "ai_infrastructure": ["ai_infra", "semis"],
        "cloud_saas": ["cloud_saas"],
        "cybersecurity": ["cybersecurity"],
        "technology": ["mega_tech", "cloud_saas", "ai_software"],
        "ai_software": ["ai_software"],
    }
    mapped_clusters = SECTOR_CLUSTERS.get(sector.lower(), [])
    for cluster_name in mapped_clusters:
        if cluster_name in PRESET_CLUSTERS:
            sector_tickers.extend(PRESET_CLUSTERS[cluster_name])

    sector_tickers = list(set(sector_tickers))

    current_sector_pct = sum(
        pct for t, pct in held_positions.items()
        if t in sector_tickers or t == ticker
    )
    projected_sector_pct = current_sector_pct + ASSUMED_POSITION_PCT

    # Find correlated tickers already held
    correlated_tickers = [t for t in held_positions if t in sector_tickers and t != ticker]

    warning = None
    block = False

    if projected_sector_pct > SECTOR_CAP:
        block = True
        warning = (
            f"Sector '{sector}' would reach {projected_sector_pct:.0%} exposure "
            f"(limit {SECTOR_CAP:.0%}) — position blocked. "
            f"Already holding: {', '.join(correlated_tickers) or 'none'}"
        )
    elif projected_sector_pct > SECTOR_CAP * 0.80:
        warning = (
            f"Sector '{sector}' approaching limit: {current_sector_pct:.0%} → "
            f"{projected_sector_pct:.0%} (limit {SECTOR_CAP:.0%}). "
            f"Correlated positions: {', '.join(correlated_tickers) or 'none'}"
        )

    return {
        "current_sector_pct": round(current_sector_pct, 3),
        "projected_sector_pct": round(projected_sector_pct, 3),
        "correlated_tickers": correlated_tickers,
        "warning": warning,
        "block": block,
    }


def portfolio_correlation_report(
    held_positions: dict[str, float],  # {ticker: pct}
    top_n_pairs: int = 10,
) -> dict:
    """
    Returns a full correlation report for the current portfolio.
    Useful for dashboard: shows riskiest correlated pairs and cluster exposures.
    """
    tickers = list(held_positions.keys())
    if len(tickers) < 2:
        return {"clusters": [], "top_pairs": [], "tickers": tickers}

    returns = fetch_returns(tickers)
    corr_map = compute_correlation_matrix(returns)

    # Top correlated pairs
    pairs = sorted(
        [{"a": a, "b": b, "corr": c} for (a, b), c in corr_map.items()],
        key=lambda x: -x["corr"],
    )[:top_n_pairs]

    # Cluster exposures
    clusters = build_dynamic_clusters(corr_map)
    cluster_exposures = []
    for cluster in clusters:
        exp = sum(held_positions.get(t, 0) for t in cluster)
        cluster_exposures.append({
            "members": sorted(cluster),
            "total_exposure_pct": round(exp, 3),
            "limit": CLUSTER_MAX_PCT,
            "over_limit": exp > CLUSTER_MAX_PCT,
        })
    cluster_exposures.sort(key=lambda x: -x["total_exposure_pct"])

    # Also check preset clusters
    preset_exposures = []
    for name, members in PRESET_CLUSTERS.items():
        exp = sum(held_positions.get(t, 0) for t in members)
        if exp > 0:
            preset_exposures.append({
                "cluster": name,
                "members_held": [t for t in members if t in held_positions],
                "total_exposure_pct": round(exp, 3),
                "limit": CLUSTER_MAX_PCT,
                "over_limit": exp > CLUSTER_MAX_PCT,
            })
    preset_exposures.sort(key=lambda x: -x["total_exposure_pct"])

    return {
        "tickers": tickers,
        "top_pairs": pairs,
        "dynamic_clusters": cluster_exposures,
        "preset_clusters": preset_exposures,
    }
