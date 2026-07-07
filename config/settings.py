"""
Central configuration. Set API keys via environment variables.
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Settings:
    # --- API Keys (set in environment) ---
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_KEY", "")
    perplexity_api_key: str = os.getenv("PERPLEXITY_API_KEY", "")
    news_api_key: str = os.getenv("NEWS_API_KEY", "")

    # --- Data Layer ---
    price_source: str = "yfinance"          # MVP: free. Upgrade: "iex"
    fundamentals_source: str = "sec_edgar"  # MVP: free. Upgrade: "factset"
    sentiment_model: str = "vader"          # MVP. Upgrade: "finbert"

    # --- Staleness Thresholds (days) ---
    staleness_green: int = 7
    staleness_yellow: int = 30
    staleness_red: int = 90

    # --- Conviction Thresholds ---
    max_sentiment_boost: float = 1.5   # cap on sentiment's contribution to conviction (raw boost is ±5)
    buy_conviction_min: int = 8
    buy_confidence_min: int = 7
    watchlist_conviction_min: int = 6
    watchlist_confidence_min: int = 6

    # --- Position Sizing ---
    max_position_high_conviction: float = 0.07   # 7%
    max_position_med_conviction: float = 0.04    # 4%
    max_sector_pct: float = 0.25                 # 25%
    max_ai_infra_pct: float = 0.35               # 35%
    max_semis_pct: float = 0.30                  # 30%
    min_cash_pct: float = 0.10                   # 10%

    # --- Risk Per Trade (Batch B) ---
    max_risk_per_trade: float = 0.01         # LT: max % of portfolio lost if the hard stop hits
    swing_max_risk_per_trade: float = 0.015  # swing pool: same idea, wider for 6-slot book
    swing_earnings_blackout_days: int = 10   # no swing entries this close to an earnings print
    swing_max_per_macro_sector: int = 2      # max swing slots (of 6) in one macro sector

    # --- Stop Loss ATR Multipliers ---
    stop_tier1_atr: float = 2.5
    stop_tier2_atr: float = 3.5
    stop_tier3_atr: float = 4.5

    # --- Regime Detection ---
    bull_spy_threshold: float = 0.20   # S&P +20% YTD
    bear_spy_threshold: float = -0.10  # S&P -10%
    bull_vix_max: float = 20.0
    bear_vix_min: float = 25.0

    # --- Liquidity Minimums ---
    min_market_cap: float = 100e6   # $100M
    min_daily_volume: float = 50e6  # $50M

    # --- Drift Detection ---
    drift_lookback_days: int = 30
    drift_alert_threshold: float = -0.03   # -3% hit rate drop

    # --- Dashboard ---
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000

    # --- Storage ---
    use_supabase: bool = bool(os.getenv("SUPABASE_URL"))
    local_data_dir: str = "data"


settings = Settings()
