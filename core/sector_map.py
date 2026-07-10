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
    # 11th macro added 2026-07-09 — the defensive-rotation blind spot: staples
    # led 2022 while tech bled, and the funnel couldn't see it as a theme
    "consumer_staples":         {"etf": "XLP"},
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
        # etf was None → scout silently SKIPPED this micro since creation (found
        # 2026-07-09 adding ai_photonics); MU = largest pure-play price proxy
        "macro": "technology",
        "etf": "MU",
        "proxy_stock": "MU",
        "stocks": ["MU", "WDC", "STX", "AMAT"],
    },
    "ai_photonics": {
        # optical interconnects / photonics for AI datacenters — added 2026-07-09
        # (was a funnel blind spot: LITE/COHR/CIEN were swing-scannable but no
        # microsector meant the weekly funnel and LT track never saw the theme)
        "macro": "technology",
        # no liquid pure-photonics ETF exists; LITE (largest pure-play) is the
        # price proxy — scout SKIPS etf=None micros entirely, it does not proxy
        "etf": "LITE",
        "stocks": ["LITE", "COHR", "CIEN", "FN", "MRVL", "ANET"],
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
    "streaming_media": {
        # added 2026-07-09 — comm services previously had ONE micro; NFLX only
        # survived as a wildcard. No clean streaming ETF → NFLX price proxy
        # (LITE/ai_photonics precedent; scout skips etf=None)
        "macro": "communication_services",
        "etf": "NFLX",
        "stocks": ["NFLX", "DIS", "SPOT", "WBD"],
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
    "insurance": {
        # added 2026-07-09 coverage audit — quietly a top sector of 2023-24,
        # zero funnel representation before
        "macro": "financials",
        "etf": "KIE",
        "stocks": ["PGR", "ALL", "TRV", "AIG", "MET", "CB"],
    },
    "crypto_financials": {
        # added 2026-07-09 — hot 2025-26 theme, previously swing-scan-only
        "macro": "financials",
        "etf": "DAPP",
        "stocks": ["COIN", "HOOD", "MSTR"],
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
    "managed_care": {
        # added 2026-07-09 — huge theme, moves independently of pharma/biotech
        "macro": "healthcare",
        "etf": "IHF",
        "stocks": ["UNH", "ELV", "CI", "HUM", "CNC", "CVS"],
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
    "power_grid": {
        # electrification / AI-power buildout — added 2026-07-09: the theme was
        # scattered across nuclear (CEG/VST), ai_infrastructure (VRT), utilities
        # and infrastructure (PWR); GEV/ETN belonged to no microsector at all,
        # so the funnel could never rank "power" as one unit
        "macro": "industrials",
        "etf": "GRID",
        "stocks": ["GEV", "ETN", "VRT", "PWR", "HUBB", "CEG", "VST"],
    },
    "transports_airlines": {
        # added 2026-07-09 — classic cyclical leader (airlines ripped 2024-25)
        "macro": "industrials",
        "etf": "IYT",
        "stocks": ["UAL", "DAL", "UNP", "ODFL", "UPS", "CSX"],
    },

    # ── Consumer Discretionary ──
    "ecommerce": {
        "macro": "consumer_discretionary",
        "etf": "IBUY",
        "stocks": ["AMZN", "SHOP", "ETSY", "MELI", "SE", "PDD"],
    },
    "travel_leisure": {
        # added 2026-07-09 — led whole stretches of 2023-25
        "macro": "consumer_discretionary",
        "etf": "PEJ",
        "stocks": ["BKNG", "MAR", "HLT", "RCL", "UBER", "ABNB"],
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
    "midstream_natgas": {
        # added 2026-07-09 — natgas/pipelines riding the AI-power buildout,
        # adjacent to power_grid
        "macro": "energy",
        "etf": "AMLP",
        "stocks": ["WMB", "KMI", "OKE", "LNG"],
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
    "staples": {
        # added 2026-07-09 with the consumer_staples macro — the defensive theme
        "macro": "consumer_staples",
        "etf": "XLP",
        "stocks": ["PG", "KO", "PEP", "WMT", "COST", "PM", "MDLZ"],
    },
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
