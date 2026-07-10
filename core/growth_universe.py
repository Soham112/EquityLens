"""
Growth Scout Universe — curated list of small/mid-cap growth tickers.

INCLUSION CRITERIA (all must be met):
  1. Market cap $300M–$10B — below $300M is too illiquid; above $10B is large-cap territory
  2. Avg daily dollar volume >$3M — you must be able to enter and exit without moving the price
  3. Revenue growth >20% YoY minimum — below this it is not a growth story
  4. Gross margin >30% — thin-margin commodity businesses don't compound
  5. Operates in a sector with structural multi-year tailwind (AI infra, semis, biotech, etc.)
  6. Has a technology moat, patent position, or niche bottleneck — not a commodity
  7. Not already in the main S&P 500 / Nasdaq 100 universe (no overlap with main scanner)

EXCLUSION — remove immediately if:
  - Delisted or halted (yfinance returns no data)
  - Revenue declining two consecutive quarters
  - Cash runway <4 quarters with no financing in sight
  - Market cap drops below $200M (liquidity risk)

HOW TO ADD TICKERS:
  Add a line: ("TICKER", "sector") — sectors must match SECTOR_ETF_MAP keys in growth_hunter.py
  The system picks up new tickers on the next scan automatically.

Organized by sector for sector-tailwind scoring.
"""

GROWTH_UNIVERSE: list[tuple[str, str]] = [
    # ── AI Infrastructure & Semiconductors ──
    ("NVTS",  "semiconductors"),    # Navitas — GaN power semiconductors for AI servers
    ("AOSL",  "semiconductors"),    # Alpha & Omega — power delivery chips for AI racks
    ("ACLS",  "semiconductors"),    # Axcelis — ion implant equipment for HBM/memory fabs
    ("MRAM",  "semiconductors"),    # Everspin — MRAM chips, aerospace/industrial edge AI
    ("AEHR",  "semiconductors"),    # Aehr Test Systems — wafer-level burn-in for EV/AI chips
    ("AMBA",  "semiconductors"),    # Ambarella — AI vision chips for edge/surveillance
    ("CEVA",  "semiconductors"),    # CEVA — semiconductor IP for AI/wireless
    ("FORM",  "semiconductors"),    # FormFactor — semiconductor probe cards
    ("MKSI",  "semiconductors"),    # MKS Instruments — process control for chip fabs
    ("ONTO",  "semiconductors"),    # Onto Innovation — semiconductor inspection
    ("POWI",  "semiconductors"),    # Power Integrations — power conversion ICs
    ("SITM",  "semiconductors"),    # SiTime — precision timing semiconductors
    ("SMTC",  "semiconductors"),    # Semtech — IoT/data center signal integrity chips

    # ── AI Photonics (optical interconnects for AI datacenters) ──
    ("AAOI",  "ai_photonics"),      # Applied Optoelectronics — optical transceivers
    ("POET",  "ai_photonics"),      # POET Technologies — optical interposers for AI

    # ── AI Software & Infrastructure ──
    ("BBAI",  "ai_infrastructure"), # BigBear.ai — AI analytics for defense/government
    ("CXAI",  "ai_infrastructure"), # CXApp — AI workplace intelligence
    ("SOUN",  "ai_infrastructure"), # SoundHound — voice AI platform

    # ── Cloud & Cybersecurity ──
    ("ATEN",  "cybersecurity"),     # A10 Networks — application delivery/cybersecurity
    ("IRTC",  "cybersecurity"),     # iRhythm — digital health (IoT med device)
    ("TASK",  "software"),          # TaskUs — AI-enabled BPO
    ("RSKD",  "software"),          # Riskified — e-commerce fraud prevention AI
    ("EVTC",  "software"),          # Evertec — fintech payments Latin America
    ("RELY",  "fintech"),           # Remitly — digital remittance platform
    ("STEP",  "fintech"),           # StepStone — private markets fintech
    ("NRDS",  "fintech"),           # NerdWallet — personal finance platform

    # ── Biotech & Genomics ──
    ("RXRX",  "genomics"),          # Recursion Pharma — AI drug discovery
    ("SEER",  "genomics"),          # Seer Bio — proteomics for drug discovery
    ("PACB",  "genomics"),          # Pacific Biosciences — long-read DNA sequencing
    ("BEAM",  "genomics"),          # Beam Therapeutics — base editing gene therapy
    ("RLAY",  "biotech"),           # Relay Therapeutics — AI drug discovery
    ("NUVL",  "biotech"),           # Nuvalent — precision oncology
    ("KYMR",  "biotech"),           # Kymera Therapeutics — targeted protein degradation
    ("PRCT",  "biotech"),           # Procept BioRobotics — robotic surgery systems

    # ── Clean Energy & EV ──
    ("CHPT",  "ev"),                # ChargePoint — EV charging network
    ("BLNK",  "ev"),                # Blink Charging — EV charging infrastructure
    ("AMPX",  "ev"),                # Amprius — silicon anode batteries for aviation/EV
    ("MVST",  "ev"),                # Microvast — fast-charge EV battery systems
    ("ARRY",  "clean_energy"),      # Array Technologies — solar tracker systems
    ("STEM",  "clean_energy"),      # Stem Inc — AI-driven battery storage
    ("OPAL",  "clean_energy"),      # OPAL Fuels — renewable natural gas
    ("SPWR",  "clean_energy"),      # SunPower — residential solar
    ("FLNC",  "clean_energy"),      # Fluence Energy — grid-scale energy storage AI

    # ── Space & Defense Tech ──
    ("MNTS",  "space"),             # Momentus — in-space transportation services
    ("ASTS",  "space"),             # AST SpaceMobile — satellite broadband
    ("RKLB",  "space"),             # Rocket Lab — small launch vehicles + spacecraft
    ("PL",    "space"),             # Planet Labs — daily satellite earth imaging
    ("SPIR",  "space"),             # Spire Global — satellite data analytics
    ("KTOS",  "defense"),           # Kratos Defense — unmanned systems/drones
    ("AVAV",  "defense"),           # AeroVironment — tactical drones
    ("HNST",  "defense"),           # (placeholder)

    # ── Materials & Memory ──
    ("MRAM",  "semiconductors"),    # Everspin (already above, dedup handled in build)
    ("RMBS",  "semiconductors"),    # Rambus — memory interface chips/IP licensing
    ("CRUS",  "semiconductors"),    # Cirrus Logic — audio/mixed-signal ICs for Apple
    ("DIOD",  "semiconductors"),    # Diodes Inc — discrete semiconductors

    # ── Industrial AI & Robotics ──
    ("NNDM",  "industrials"),       # Nano Dimension — 3D printing electronics
    ("VNET",  "industrials"),       # VNET Group — China data centers
    ("SERA",  "industrials"),       # Sera Prognostics — AI maternal health diagnostics

    # ── Quantum Computing ──
    ("QUBT",  "technology"),        # Quantum Computing Inc
    ("IONQ",  "technology"),        # IonQ — trapped ion quantum computing
    ("RGTI",  "technology"),        # Rigetti Computing — superconducting quantum
]

# Deduplicate while preserving order
_seen: set[str] = set()
_deduped: list[tuple[str, str]] = []
for _t, _s in GROWTH_UNIVERSE:
    if _t not in _seen:
        _seen.add(_t)
        _deduped.append((_t, _s))
GROWTH_UNIVERSE = _deduped


def get_growth_universe() -> list[tuple[str, str]]:
    """Return the full curated growth universe as (ticker, sector) pairs."""
    return list(GROWTH_UNIVERSE)


def add_ticker(ticker: str, sector: str) -> None:
    """Add a ticker to the in-memory universe for this session."""
    global GROWTH_UNIVERSE
    if not any(t == ticker for t, _ in GROWTH_UNIVERSE):
        GROWTH_UNIVERSE.append((ticker.upper(), sector.lower()))


def get_growth_names() -> dict[str, str]:
    """
    Ticker → company name, parsed from this file's own inline comments
    (format: ("TICK", "sector"),  # Company Name — description).
    These small/mid-caps aren't in the S&P/Nasdaq name tables, and the
    comment is already the maintained source of truth for what each is.
    """
    import re
    from pathlib import Path

    names: dict[str, str] = {}
    try:
        src = Path(__file__).read_text()
        for m in re.finditer(r'\("([A-Z.\-]+)",\s*"[a-z_]+"\),?\s*#\s*([^—\n]+)', src):
            ticker, name = m.group(1), m.group(2).strip()
            if name and ticker not in names:
                names[ticker] = name
    except Exception:
        pass
    return names
