"""
Orchestrator — chains all 7 agents for a single stock analysis run.

Data strategy:
  - Price / ATR / RSI / technicals → yfinance (real-time, standalone)
  - Fundamentals / sentiment / analyst ratings → Bigdata.com cache
  - Insider/risk flags → derived from BigData sentiment docs
  - Cache refreshed by: python workflows/bigdata_refresh.py
"""
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

from agents import chart_vision, critic, hunter, portfolio_manager, scout, sentiment, validator
from core import vision_cache
from agents.portfolio_manager import PortfolioState
from config.settings import settings
from core import bigdata_client
from core.conviction_monitor import DropAction, check_conviction_drop
from core.persistence import (ConvictionSnapshot, get_previous_convictions,
                               record_convictions, sync_convictions_to_supabase,
                               get_conviction_series)
from core.data_layer import (FundamentalsData, InsiderData, fetch_fundamentals,
                             fetch_insider_data, fetch_price_data)
from core.earnings_calendar import EarningsPhase, apply_earnings_stop_widening, check_earnings_gate
from core.regime_detector import MarketRegime, RegimeResult, detect_regime
from core.staleness import StalenessResult, check_data_staleness, cache_age_days_to_quality
from core.stop_loss import apply_vix_spike_widening

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    ticker: str
    timestamp: str
    signal: str
    conviction: float
    data_confidence: float
    hunter_score: float
    sentiment_boost: float
    data_quality: str
    red_flags: list[str]
    kill_switch: bool
    sector: str
    sector_status: str
    regime: str
    stop_tier1: Optional[float]
    stop_tier2: Optional[float]
    stop_tier3: Optional[float]
    recommended_position_pct: float
    recommended_position_dollars: float
    alerts: list[str]
    thesis: str
    key_risks: list[str]
    requires_human_review: bool
    data_sources: dict    # which sources contributed
    earnings_phase: str   # GAP 10: NORMAL | EARNINGS_WATCH | EARNINGS_CAUTION | EARNINGS_BLACKOUT
    days_to_earnings: Optional[int]   # None if unknown
    lt_chart: Optional[object] = None  # SwingChartSignal from long-term chart analysis (BUY only)
    # Upgrade 1: Intrinsic value gate
    fair_value_low: Optional[float] = None
    fair_value_high: Optional[float] = None
    margin_of_safety: Optional[float] = None
    # Upgrade 7: Macro overlay
    macro_headwinds: Optional[list] = None


def _merge_fundamentals(
    yf_fund: Optional[FundamentalsData],
    bd_fund: Optional[bigdata_client.BigDataFundamentals],
    ticker: str,
) -> FundamentalsData:
    """
    BigData takes precedence for margins/PE/analyst data.
    yfinance fills revenue_growth, FCF, D/E (BigData doesn't provide these directly).
    """
    if yf_fund is None and bd_fund is None:
        return FundamentalsData(
            ticker=ticker, revenue_growth_yoy=None, fcf=None,
            gross_margin=None, debt_to_equity=None, pe_ratio=None,
            peg_ratio=None, ev_to_fcf=None, last_earnings_date=None,
            last_updated=datetime.date.today(),
        )

    base = yf_fund or FundamentalsData(
        ticker=ticker, revenue_growth_yoy=None, fcf=None,
        gross_margin=None, debt_to_equity=None, pe_ratio=None,
        peg_ratio=None, ev_to_fcf=None, last_earnings_date=None,
        last_updated=datetime.date.today(),
    )

    if bd_fund:
        # Override with BigData where it's more reliable
        if bd_fund.gross_margin is not None:
            base.gross_margin = bd_fund.gross_margin
        if bd_fund.pe_ratio is not None:
            base.pe_ratio = bd_fund.pe_ratio

    return base


def _build_critic_flags(
    bd_insider: Optional[bigdata_client.BigDataInsider],
    extra: Optional[dict],
) -> dict:
    flags = dict(extra or {})
    if bd_insider:
        if bd_insider.litigation_flag:
            flags["litigation"] = True
        if bd_insider.sec_investigation_flag:
            flags["sec_investigation"] = True
        if bd_insider.auditor_warning_flag:
            flags["auditor_warning"] = True
        if bd_insider.export_control_flag:
            flags.setdefault("export_control_noted", True)
    return flags


def analyze(
    ticker: str,
    sector: str,
    portfolio: Optional[PortfolioState] = None,
    regime: Optional[RegimeResult] = None,
    extra_critic_flags: Optional[dict] = None,
    prev_conviction: Optional[float] = None,   # GAP 12: yesterday's conviction for held positions
    macro_pulse=None,   # Upgrade 7: MacroPulse object (pre-computed in run_batch)
) -> Optional[AnalysisResult]:

    logger.info(f"Analyzing {ticker}...")
    portfolio = portfolio or PortfolioState()
    regime = regime or detect_regime()
    alerts = []
    data_sources = {"price": "yfinance", "fundamentals": "yfinance", "sentiment": "none"}

    # ── Price data (always yfinance) ──
    price = fetch_price_data(ticker)
    if price is None:
        logger.warning(f"{ticker}: no price data — skipping")
        return None

    # ── BigData cache ──
    bd_fund = bigdata_client.get_fundamentals(ticker)
    bd_sent = bigdata_client.get_sentiment(ticker)
    bd_insider = bigdata_client.get_insider_flags(ticker)

    cache_age = bigdata_client.cache_age_days(ticker)
    if bd_fund:
        data_sources["fundamentals"] = "bigdata"
    if bd_sent:
        data_sources["sentiment"] = "bigdata"
    if cache_age is not None and cache_age > 1:
        alerts.append(f"BigData cache {cache_age}d old — consider refreshing")

    # ── yfinance fundamentals (fills FCF, revenue growth, D/E) ──
    yf_fund = fetch_fundamentals(ticker)
    fundamentals = _merge_fundamentals(yf_fund, bd_fund, ticker)

    # ── Insider data ──
    if bd_insider:
        insider = bigdata_client.to_insider_data(ticker, bd_insider)
        data_sources["insider"] = "bigdata"
    else:
        insider = fetch_insider_data(ticker)
        data_sources["insider"] = "yfinance"

    # ── Staleness [GAP 1] ──
    last_update = fundamentals.last_updated
    staleness: StalenessResult = check_data_staleness(
        ticker=ticker,
        last_fundamental_update=last_update,
        red_flag_count=0,
    )
    if staleness.alert:
        alerts.append(staleness.alert)

    if not staleness.should_score:
        logger.warning(f"{ticker}: {staleness.status}")
        return AnalysisResult(
            ticker=ticker, timestamp=datetime.datetime.now().isoformat(),
            signal="AVOID", conviction=0, data_confidence=0,
            hunter_score=0, sentiment_boost=0,
            data_quality=staleness.data_quality.value,
            red_flags=[staleness.status], kill_switch=False,
            sector=sector, sector_status="UNKNOWN", regime=regime.regime.value,
            stop_tier1=None, stop_tier2=None, stop_tier3=None,
            recommended_position_pct=0, recommended_position_dollars=0,
            alerts=alerts, thesis="Data too stale to score",
            key_risks=[staleness.alert or staleness.status],
            requires_human_review=False, data_sources=data_sources,
            earnings_phase="NORMAL", days_to_earnings=None,
        )

    # ── GAP 10: Earnings calendar gate ──
    earnings = check_earnings_gate(ticker)
    if earnings.alert:
        alerts.append(earnings.alert)

    # ── GAP 7: VIX spike — pause new buys [GAP 7] ──
    if regime.new_buys_paused:
        alerts.append(
            f"VIX SPIKE ({regime.vix_level:.0f}) — new buy signals paused. "
            "Holding existing positions with widened stops."
        )

    # ── Upgrade 1: Intrinsic Value Gate ──
    valuation_result = None
    try:
        from core.valuation import estimate_fair_value
        valuation_result = estimate_fair_value(ticker, fundamentals, sector)
    except Exception as e:
        logger.debug(f"[Orchestrator] {ticker} valuation gate skipped: {e}")

    # ── Agent pipeline ──
    hunter_result = hunter.run(price, fundamentals)

    # Chart vision: pattern detection + MA crossover context + price structure
    # Uses cache — only re-runs when price breaks S/R, volume spikes, MA crosses, or cache expires
    # Weekly chart: score >= 6; daily chart: score >= 8 (timing confirmation)
    vision_result = None
    if hunter_result.score >= 6:
        vision_result = vision_cache.get_or_fetch(
            ticker=ticker, timeframe="weekly",
            period="5y", interval="1wk",
            current_price=price.current_price,
        )
    if hunter_result.score >= 8:
        daily_result = vision_cache.get_or_fetch(
            ticker=ticker, timeframe="daily",
            period="1y", interval="1d",
            current_price=price.current_price,
        )
        # Combine: weekly sets structure context (40%), daily drives timing (60%)
        if vision_result and daily_result:
            combined_delta = round(
                vision_result.chart_score_delta * 0.4 + daily_result.chart_score_delta * 0.6, 2
            )
            vision_result = vision_result
            vision_result.chart_score_delta = combined_delta
        elif daily_result:
            vision_result = daily_result

    if vision_result and vision_result.chart_score_delta != 0.0:
        original_score = hunter_result.score
        hunter_result.score = round(
            max(0.0, min(10.0, hunter_result.score + vision_result.chart_score_delta)), 2
        )
        hunter_result.technicals_score = round(
            max(0.0, hunter_result.technicals_score + vision_result.chart_score_delta), 2
        )
        if vision_result.pattern != "none":
            hunter_result.flags.append(
                f"Chart pattern: {vision_result.pattern.replace('_', ' ')} "
                f"({vision_result.pattern_confidence:.0%} confidence) — "
                f"{vision_result.price_structure}, "
                f"MA crossover: {vision_result.ma_crossover_type}/{vision_result.ma_crossover_recency}"
            )
        logger.info(
            f"[Orchestrator] {ticker} hunter score adjusted by chart vision: "
            f"{original_score:.1f} → {hunter_result.score:.1f} (delta={vision_result.chart_score_delta:+.2f})"
        )

    critic_flags = _build_critic_flags(bd_insider, extra_critic_flags)
    critic_result = critic.run(price, fundamentals, insider, critic_flags)
    sentiment_result = sentiment.run(ticker=ticker)
    from core.sector_map import MACRO_SECTORS, MICRO_SECTORS
    _sector_etf = (
        MACRO_SECTORS.get(sector, {}).get("etf")
        or MICRO_SECTORS.get(sector, {}).get("etf")
        or "SPY"
    )
    sector_assessment = scout.assess_sector(sector, _sector_etf)
    validator_result = validator.run(
        hunter=hunter_result,
        sentiment=sentiment_result,
        critic=critic_result,
        staleness=staleness,
        sector_penalty=sector_assessment.conviction_penalty,
    )

    # ── GAP 15: Sector gate — hard signal cap based on sector momentum ──
    conv_result = validator_result.conviction
    effective_signal = conv_result.signal

    # ── Upgrade 1: Apply valuation conviction cap ──
    if valuation_result and valuation_result.conviction_cap is not None:
        if conv_result.conviction > valuation_result.conviction_cap:
            mos_pct = f"{valuation_result.margin_of_safety:+.0%}"
            alerts.append(
                f"Valuation gate: conviction capped at {valuation_result.conviction_cap:.1f} "
                f"({valuation_result.verdict}, MOS={mos_pct})"
            )
            conv_result = conv_result.__class__(
                **{**conv_result.__dict__, "conviction": valuation_result.conviction_cap}
            )
            # Re-evaluate signal after cap — BUY requires conviction >= 8, so an
            # OVERVALUED cap to 7.0 must demote (was silently auto-buying capped names)
            if conv_result.conviction < 8 and effective_signal == "BUY":
                effective_signal = "WATCHLIST"
                alerts.append("Valuation gate: BUY → WATCHLIST (capped conviction below BUY threshold)")
    if sector_assessment.signal == "WAIT":
        # MAJOR_HEADWIND (sector down >25% in 60d) or BUBBLE: block new buys entirely
        if effective_signal == "BUY":
            effective_signal = "WATCHLIST"
            alerts.append(
                f"Sector gate: BUY → WATCHLIST — {sector_assessment.status} "
                f"({sector_assessment.notes})"
            )
        elif effective_signal == "WATCHLIST":
            effective_signal = "AVOID"
            alerts.append(
                f"Sector gate: WATCHLIST → AVOID — {sector_assessment.status} "
                f"({sector_assessment.notes})"
            )
    elif sector_assessment.signal == "WATCHLIST":
        # HEADWIND (sector down 15-25%): no new buys; existing watchlists stay
        if effective_signal == "BUY":
            effective_signal = "WATCHLIST"
            alerts.append(
                f"Sector gate: BUY → WATCHLIST — {sector_assessment.status} "
                f"({sector_assessment.notes})"
            )

    # ── Upgrade 3: Entry correlation check ──
    if effective_signal == "BUY":
        try:
            from core.correlation import entry_correlation_check
            corr_check = entry_correlation_check(ticker, sector, portfolio)
            if corr_check["block"]:
                effective_signal = "WATCHLIST"
                alerts.append(f"Correlation gate: BUY → WATCHLIST — {corr_check['warning']}")
            elif corr_check["warning"]:
                alerts.append(f"Correlation warning: {corr_check['warning']}")
        except Exception as e:
            logger.debug(f"[Orchestrator] {ticker} correlation check skipped: {e}")

    # ── Apply earnings conviction cap (forces WATCHLIST during caution/blackout) ──
    if earnings.conviction_cap is not None and conv_result.conviction > earnings.conviction_cap:
        if effective_signal == "BUY":
            effective_signal = "WATCHLIST"
            alerts.append(
                f"Signal downgraded BUY → WATCHLIST: earnings {earnings.phase.value} "
                f"({earnings.days_to_earnings}d out)"
            )
    elif earnings.block_new_buy and effective_signal == "BUY":
        effective_signal = "WATCHLIST"

    # ── GAP 7: Block new buys on VIX spike ──
    if regime.new_buys_paused and effective_signal == "BUY":
        effective_signal = "WATCHLIST"

    # ── GAP 12: Conviction drop response for held positions ──
    drop_result = None
    if prev_conviction is not None:
        drop_result = check_conviction_drop(ticker, prev_conviction, conv_result.conviction)
        if drop_result.alert:
            alerts.append(drop_result.alert)
        if drop_result.action.value.startswith("TRIM") or drop_result.action == DropAction.EXIT:
            effective_signal = drop_result.action.value

    # ── Conviction trend: rising conviction = extra signal strength ──
    try:
        series = get_conviction_series(ticker, days=30)
        if len(series) >= 5:
            recent_avg = sum(s["conviction"] for s in series[-3:]) / 3
            prior_avg  = sum(s["conviction"] for s in series[:3]) / 3
            trend_delta = recent_avg - prior_avg
            if trend_delta >= 1.5:
                alerts.append(f"Conviction trending UP +{trend_delta:.1f} over last 30 days — strengthening thesis")
                conv_result = conv_result.__class__(
                    **{**conv_result.__dict__, "conviction": min(conv_result.conviction + 0.3, 10.0)}
                )
            elif trend_delta <= -1.5:
                alerts.append(f"Conviction trending DOWN {trend_delta:.1f} over last 30 days — thesis weakening")
                conv_result = conv_result.__class__(
                    **{**conv_result.__dict__, "conviction": max(conv_result.conviction - 0.3, 0.0)}
                )
                # Re-evaluate signal after penalty — same rule as valuation/macro gates
                if conv_result.conviction < 8 and effective_signal == "BUY":
                    effective_signal = "WATCHLIST"
                    alerts.append("Conviction trend: BUY → WATCHLIST (adjusted conviction below BUY threshold)")
    except Exception:
        pass

    # ── Upgrade 7: Macro overlay penalty ──
    macro_headwinds = []
    if macro_pulse and effective_signal == "BUY" and macro_pulse.conviction_penalty > 0:
        new_conviction = max(0.0, conv_result.conviction - macro_pulse.conviction_penalty)
        macro_headwinds = macro_pulse.headwinds
        if macro_pulse.headwind_count >= 2:
            alerts.append(
                f"Macro headwinds ({macro_pulse.headwind_count}): "
                f"{'; '.join(macro_pulse.headwinds[:2])} "
                f"— conviction -{macro_pulse.conviction_penalty:.1f}"
            )
        conv_result = conv_result.__class__(
            **{**conv_result.__dict__, "conviction": round(new_conviction, 1)}
        )
        # Re-evaluate signal after penalty
        if conv_result.conviction < 8 and effective_signal == "BUY":
            effective_signal = "WATCHLIST"

    # ── Mistake-pattern penalty: learned from our own closed losses ──
    # Same design as the macro penalty: bounded, evidence-gated, and only
    # fires when THIS candidate matches the losing pattern's entry conditions.
    if effective_signal == "BUY":
        try:
            from core.feedback import mistake_conviction_penalty
            mp, mp_reasons = mistake_conviction_penalty(
                hunter_score=hunter_result.score,
                signal_type="BUY",
                rsi=getattr(price, "rsi_14", None),
            )
            alerts.extend(mp_reasons)
            if mp > 0:
                conv_result = conv_result.__class__(
                    **{**conv_result.__dict__,
                       "conviction": round(max(0.0, conv_result.conviction - mp), 1)}
                )
                if conv_result.conviction < 8:
                    effective_signal = "WATCHLIST"
        except Exception as e:
            logger.warning(f"Mistake-pattern penalty check failed for {ticker}: {e}")

    # ── Analyst consensus boost from BigData ──
    if bd_fund and bd_fund.analyst_consensus == "Buy" and bd_fund.analyst_buy >= 10:
        if validator_result.conviction.conviction >= 6:
            alerts.append(
                f"Analyst consensus: {bd_fund.analyst_buy} Buy / "
                f"{bd_fund.analyst_hold} Hold / {bd_fund.analyst_sell} Sell — "
                f"target ${bd_fund.price_target_median:.0f}"
                if bd_fund.price_target_median else
                f"Analyst consensus: {bd_fund.analyst_consensus}"
            )

    # ── Position sizing ──
    # Size from conv_result, not validator_result: the valuation cap, conviction
    # trend, and macro penalty above all adjusted conv_result — sizing off the
    # raw validator conviction let capped 10s still take max-size positions.
    from dataclasses import replace as _dc_replace
    pos_size = portfolio_manager.recommend_position_size(
        validator=_dc_replace(validator_result, conviction=conv_result),
        portfolio=portfolio,
        regime=regime,
        sector=sector,
        price=price.current_price,
        atr=price.atr_20d,
    )
    alerts.extend(pos_size.alerts)

    # ── GAP 7 + GAP 10: Widen stops for VIX spike and earnings proximity ──
    stops = pos_size.stops
    if stops:
        if regime.vix_level > 30:
            stops = apply_vix_spike_widening(stops, regime.vix_level)
        if earnings.widen_stops_atr_mult > 0:
            stops = apply_earnings_stop_widening(stops, earnings, price.atr_20d)

    if sentiment_result.requires_human_review:
        alerts.append(f"{ticker}: high sentiment score — manual review recommended")

    # ── Thesis ──
    # conv_result carries the final conviction (valuation cap, trend, macro
    # penalty applied) — reporting validator_result.conviction here would show
    # the raw pre-cap score in results, signal records, and the thesis.
    conv = conv_result
    analyst_str = ""
    if bd_fund:
        analyst_str = f" | Analyst: {bd_fund.analyst_buy}B/{bd_fund.analyst_hold}H/{bd_fund.analyst_sell}S"
        if bd_fund.revenue_surprise_pct is not None:
            analyst_str += f" | Rev surprise {bd_fund.revenue_surprise_pct:+.1f}%"

    fallback_thesis = (
        f"Hunter={hunter_result.score:.1f} | "
        f"Sentiment={sentiment_result.sentiment_boost:+.2f} ({sentiment_result.timing_status}) | "
        f"Sector={sector_assessment.notes[:50]}{analyst_str} | "
        f"Conviction={conv.conviction:.1f} | DataConf={conv.data_confidence:.1f}"
    )

    llm_thesis = validator.enrich_thesis(
        ticker=ticker,
        signal=effective_signal,
        conviction=conv.conviction,
        hunter_score=hunter_result.score,
        sentiment_boost=sentiment_result.sentiment_boost,
        red_flags=critic_result.red_flag_labels,
        sector_notes=sector_assessment.notes,
        hunter_breakdown={
            "fundamentals": round(hunter_result.fundamentals_score, 1),
            "technicals": round(hunter_result.technicals_score, 1),
            "valuation": round(hunter_result.valuation_score, 1),
            "flags": hunter_result.flags[:6],
        },
    )
    thesis = llm_thesis or fallback_thesis

    # ── Long-term chart analysis for BUY signals ──────────────────────────────
    # Runs chart vision on 1-year weekly candles to find entry zone + S/R-anchored stop.
    lt_chart = None
    if effective_signal == "BUY":
        try:
            from core.swing_chart_analysis import analyze_longterm_candidate
            lt_chart = analyze_longterm_candidate(ticker)
            if lt_chart:
                alerts.append(
                    f"Chart: {lt_chart.entry_type} | {lt_chart.pattern} "
                    f"| entry ${lt_chart.entry_zone_low:.0f}–${lt_chart.entry_zone_high:.0f} "
                    f"| stop ${lt_chart.stop_level:.0f} (S1−0.5×ATR) "
                    f"| target ${lt_chart.target_level:.0f} | R/R {lt_chart.risk_reward:.1f}x"
                )
                # Override Tier1 stop with chart-derived stop if tighter (more meaningful)
                if stops and lt_chart.stop_level > (stops.tier1 or 0):
                    stops = stops.__class__(
                        **{**stops.__dict__, "tier1": lt_chart.stop_level}
                    )
        except Exception as e:
            logger.debug(f"[Orchestrator] {ticker} long-term chart skipped: {e}")

    return AnalysisResult(
        ticker=ticker,
        timestamp=datetime.datetime.now().isoformat(),
        signal=effective_signal,
        conviction=conv.conviction,
        data_confidence=conv.data_confidence,
        hunter_score=hunter_result.score,
        sentiment_boost=sentiment_result.sentiment_boost,
        data_quality=staleness.data_quality.value,
        red_flags=critic_result.red_flag_labels,
        kill_switch=critic_result.kill_switch,
        sector=sector,
        sector_status=sector_assessment.status,
        regime=regime.regime.value,
        stop_tier1=stops.tier1 if stops else None,
        stop_tier2=stops.tier2 if stops else None,
        stop_tier3=stops.tier3 if stops else None,
        earnings_phase=earnings.phase.value,
        days_to_earnings=earnings.days_to_earnings,
        recommended_position_pct=pos_size.recommended_pct,
        recommended_position_dollars=pos_size.recommended_dollars,
        alerts=alerts,
        thesis=thesis,
        key_risks=critic_result.red_flag_labels,
        requires_human_review=sentiment_result.requires_human_review,
        data_sources=data_sources,
        lt_chart=lt_chart,
        fair_value_low=valuation_result.fair_value_low if valuation_result else None,
        fair_value_high=valuation_result.fair_value_high if valuation_result else None,
        margin_of_safety=valuation_result.margin_of_safety if valuation_result else None,
        macro_headwinds=macro_headwinds if macro_headwinds else None,
    )


def run_batch(
    tickers: list[tuple[str, str]],
    portfolio: Optional[PortfolioState] = None,
) -> list[AnalysisResult]:
    regime = detect_regime()
    logger.info(f"Regime: {regime.regime.value} (SPY={regime.spy_ytd_return:.1%}, VIX={regime.vix_level:.1f})")
    portfolio = portfolio or PortfolioState()

    # Upgrade 7: Compute macro pulse once per batch
    macro_pulse = None
    try:
        from core.macro_pulse import get_macro_pulse
        macro_pulse = get_macro_pulse()
        logger.info(f"[MacroPulse] {macro_pulse.note}")
    except Exception as e:
        logger.debug(f"[MacroPulse] Skipped: {e}")

    # Load previous conviction scores for the conviction drop matrix
    ticker_list = [t for t, _ in tickers]
    prev_convictions = get_previous_convictions(ticker_list)
    if prev_convictions:
        logger.info(f"Loaded previous convictions for {len(prev_convictions)} tickers")

    def _analyze_one(args):
        ticker, sector = args
        try:
            return analyze(
                ticker, sector, portfolio, regime,
                prev_conviction=prev_convictions.get(ticker),
                macro_pulse=macro_pulse,
            )
        except Exception as e:
            logger.error(f"Error analyzing {ticker}: {e}")
            return None

    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_analyze_one, item): item for item in tickers}
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    # Persist today's conviction scores for tomorrow's drop matrix
    today = datetime.date.today().isoformat()
    snapshots = [
        ConvictionSnapshot(
            ticker=r.ticker,
            date=today,
            conviction=r.conviction,
            signal=r.signal,
            hunter_score=r.hunter_score,
            data_confidence=r.data_confidence,
        )
        for r in results
    ]
    record_convictions(snapshots)
    sync_convictions_to_supabase(snapshots)  # no-op if Supabase not configured

    # Upgrade 5: Log performance stats on Monday
    try:
        import datetime as _dt
        if _dt.date.today().weekday() == 0:  # Monday
            from core.signal_tracker import get_performance_stats
            stats = get_performance_stats()
            if stats.get("total_signals", 0) > 0:
                logger.info(
                    f"[SignalTracker] Performance: "
                    f"BUY hit rate={stats.get('buy_hit_rate')}, "
                    f"best sector={stats.get('best_sector')}, "
                    f"best entry={stats.get('best_entry_type')}, "
                    f"conviction accuracy={stats.get('conviction_accuracy')}"
                )
    except Exception as e:
        logger.debug(f"[SignalTracker] Stats logging skipped: {e}")

    order = {"BUY": 0, "WATCHLIST": 1, "AVOID": 2}
    results.sort(key=lambda r: (order.get(r.signal, 3), -r.conviction))
    return results
