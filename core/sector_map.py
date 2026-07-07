"""
Sector Map — single source of truth for the sector funnel.

Structure:
  MACRO_SECTORS   — 10 macro sectors with their broad ETF
  MICRO_SECTORS   — 24 microsectors: which macro they belong to, ETF proxy, candidate stocks
  WILDCARD_POOL   — stocks scanned every week regardless of sector (idiosyncratic growth)

Candidate stocks are starting pools — the weekly scan filters these down via
Hunter/Validator scoring. They are not hardcoded buy lists.
"""

# ── Macro Sectors ────────────────────────────────────────────────────────────
# etf: broad ETF used for macro-level scoring vs SPY

MACRO_SECTORS = {
    "technology":               {"etf": "XLK"},
    "communication_services":   {"etf": "XLC"},
    "financials":               {"etf": "XLF"},
    "healthcare":               {"etf": "XLV"},
    "industrials":              {"etf": "XLI"},
    "consumer_discretionary":   {"etf": "XLY"},
    "energy":                   {"etf": "XLE"},
    "utilities":                {"etf": "XLU"},
    "real_estate":              {"etf": "XLRE"},
    "materials":                {"etf": "XLB"},
}

# ── Micro Sectors ─────────────────────────────────────────────────────────────
# macro:   parent macro sector key
# etf:     ETF proxy for microsector scoring (None = use proxy_stocks[0] as price proxy)
# stocks:  candidate pool — scored by Hunter/Validator during weekly scan

MICRO_SECTORS = {
    # ── Technology ──
    "semiconductors": {
        "macro": "technology",
        "etf": "SMH",
        "stocks": ["NVDA", "AMD", "AVGO", "QCOM", "TXN", "MRVL", "AMAT", "LRCX", "KLAC", "TSM"],
    },
    "ai_infrastructure": {
        "macro": "technology",
        "etf": "SOXX",
        "stocks": ["NVDA", "AMD", "AVGO", "ARM", "MRVL", "SMCI", "VRT", "DELL"],
    },
    "memory_storage": {
        "macro": "technology",
        "etf": None,
        "proxy_stock": "MU",
        "stocks": ["MU", "WDC", "STX", "AMAT"],
    },
    "cloud_saas": {
        "macro": "technology",
        "etf": "WCLD",
        "stocks": ["DDOG", "MDB", "SNOW", "NET", "HUBS", "ZS", "CRWD", "VEEV", "NOW", "WDAY"],
    },
    "cybersecurity": {
        "macro": "technology",
        "etf": "CIBR",
        "stocks": ["CRWD", "PANW", "ZS", "NET", "S", "FTNT", "CYBR", "RPD"],
    },
    "software": {
        "macro": "technology",
        "etf": "IGV",
        "stocks": ["MSFT", "ORCL", "SAP", "ADBE", "CRM", "INTU", "NOW", "WDAY"],
    },

    # ── Communication Services ──
    "internet_social": {
        "macro": "communication_services",
        "etf": "SOCL",
        "stocks": ["META", "GOOGL", "SNAP", "PINS", "RDDT", "TTD"],
    },

    # ── Financials ──
    "banks": {
        "macro": "financials",
        "etf": "KBE",
        "stocks": ["JPM", "GS", "MS", "BAC", "WFC", "C", "USB"],
    },
    "fintech_payments": {
        "macro": "financials",
        "etf": "IPAY",
        "stocks": ["V", "MA", "PYPL", "XYZ", "FISV", "FIS", "AFRM"],
    },

    # ── Healthcare ──
    "biotech": {
        "macro": "healthcare",
        "etf": "XBI",
        "stocks": ["MRNA", "REGN", "VRTX", "RGEN", "RARE", "INCY"],
    },
    "medtech_devices": {
        "macro": "healthcare",
        "etf": "IHI",
        "stocks": ["ISRG", "MDT", "ABT", "BSX", "EW", "DXCM", "PODD"],
    },
    "pharma": {
        "macro": "healthcare",
        "etf": "XPH",
        "stocks": ["LLY", "NVO", "PFE", "ABBV", "MRK", "BMY", "ALNY"],
    },

    # ── Industrials ──
    "defense_aerospace": {
        "macro": "industrials",
        "etf": "ITA",
        "stocks": ["LMT", "RTX", "NOC", "GD", "HII", "LDOS", "PLTR"],
    },
    "robotics_automation": {
        "macro": "industrials",
        "etf": "ROBO",
        "stocks": ["ISRG", "ROK", "EMR", "ONTO", "RRX", "TDY", "KEYS"],
    },
    "infrastructure": {
        "macro": "industrials",
        "etf": "PAVE",
        "stocks": ["CAT", "DE", "VMC", "MLM", "PWR", "CARR", "TT"],
    },

    # ── Consumer Discretionary ──
    "ecommerce": {
        "macro": "consumer_discretionary",
        "etf": "IBUY",
        "stocks": ["AMZN", "SHOP", "ETSY", "MELI", "SE", "PDD"],
    },
    "ev_autos": {
        "macro": "consumer_discretionary",
        "etf": "DRIV",
        "stocks": ["TSLA", "RIVN", "NIO", "LI", "ON", "APTV"],
    },

    # ── Energy ──
    "oil_gas": {
        "macro": "energy",
        "etf": "XOP",
        "stocks": ["XOM", "CVX", "COP", "EOG", "OXY", "SLB", "HAL"],
    },
    "clean_energy": {
        "macro": "energy",
        "etf": "ICLN",
        "stocks": ["ENPH", "RUN", "NEE", "FSLR", "SEDG", "BE"],
    },
    "nuclear": {
        "macro": "energy",
        "etf": "NLR",
        "stocks": ["CCJ", "CEG", "VST", "NRG", "SMR"],
    },

    # ── Utilities ──
    "utilities": {
        "macro": "utilities",
        "etf": "XLU",
        "stocks": ["NEE", "SO", "DUK", "AEP", "EXC", "SRE"],
    },

    # ── Real Estate ──
    "reits": {
        "macro": "real_estate",
        "etf": "XLRE",
        "stocks": ["AMT", "PLD", "EQIX", "CCI", "SPG", "DLR", "O"],
    },

    # ── Materials ──
    "metals_mining": {
        "macro": "materials",
        "etf": "XME",
        "stocks": ["FCX", "NEM", "GOLD", "AA", "CLF", "MP"],
    },
    "lithium_battery": {
        "macro": "materials",
        "etf": "LIT",
        "stocks": ["ALB", "SQM", "LAC", "PLL", "ATLX"],
    },
}

# ── Wildcard Pool ─────────────────────────────────────────────────────────────
# Always scanned regardless of sector funnel result.
# For idiosyncratic growth stories, turnarounds, or stocks with exceptional fundamentals.

WILDCARD_POOL = [
    "COST",   # consistent long-term compounder, sector-agnostic
    "TSLA",   # high volatility, cross-sector impact
    "AMZN",   # cloud + retail + ads — hard to categorize cleanly
    "BRK-B",  # macro bellwether (yfinance format: dash, not dot)
    "AAPL",   # consumer + services hybrid
    "MELI",   # LatAm growth, often uncorrelated to US sector rotation
    "CELH",   # consumer growth story (reminder: sector doesn't gate everything)
    "AXON",   # defense-adjacent, consistent grower
    "CAVA",   # restaurant growth, idiosyncratic
    "DUOL",   # EdTech, no clean ETF sector
    "APP",    # ad tech, cross-sector
    "NFLX",   # streaming, no clean ETF
    "UBER",   # mobility platform, cross-sector
    "COIN",   # crypto proxy — correlated to risk-on but not a standard sector
]


# ── Helper functions ──────────────────────────────────────────────────────────

def get_microsectors_for_macro(macro_sector: str) -> dict:
    """Return all microsectors belonging to a given macro sector."""
    return {
        name: data
        for name, data in MICRO_SECTORS.items()
        if data["macro"] == macro_sector
    }


def get_stocks_for_microsector(microsector: str) -> list[str]:
    """Return candidate stock pool for a microsector."""
    return MICRO_SECTORS.get(microsector, {}).get("stocks", [])


def get_etf_for_microsector(microsector: str) -> str | None:
    """Return ETF proxy for a microsector (None if proxy_stock is used instead)."""
    data = MICRO_SECTORS.get(microsector, {})
    return data.get("etf") or data.get("proxy_stock")


def get_macro_for_stock(ticker: str) -> list[str]:
    """Return all macro sectors a ticker appears in (a stock can span multiple microsectors)."""
    macros = set()
    for ms_data in MICRO_SECTORS.values():
        if ticker in ms_data.get("stocks", []):
            macros.add(ms_data["macro"])
    return list(macros)


def get_microsectors_for_stock(ticker: str) -> list[str]:
    """Return all microsectors a ticker appears in."""
    return [
        name for name, data in MICRO_SECTORS.items()
        if ticker in data.get("stocks", [])
    ]


def to_macro(sector: str) -> str:
    """
    Normalize any sector label (microsector, macro sector, or free-form string
    like "wildcard"/"open_position") to its macro sector. Unknown labels pass
    through unchanged so they still bucket consistently.
    """
    if not sector:
        return "unknown"
    if sector in MACRO_SECTORS:
        return sector
    micro = MICRO_SECTORS.get(sector)
    if micro:
        return micro.get("macro", sector)
    return sector
