# EquityLens — AI Equity Research Platform

An intelligent, **fully automated** AI-driven equity research and paper trading platform. EquityLens combines deep fundamental analysis, technical chart vision, and real-time sentiment data to identify high-conviction investment opportunities across S&P 500 and Nasdaq 100.

## Philosophy

**Paper portfolios are fully automated** on both tracks — entries and exits execute without manual intervention. Real trades are executed manually by the user after reviewing signals. Goal: build a precise track record in paper mode, then act with confidence.

### Key Principles
- **Adaptive conviction-based sizing** — position size scales with conviction (not fixed dollar amounts)
- **Capital compounds** — exit proceeds recycle into the pool; slot sizes grow after wins, shrink after losses
- **No fixed price targets or time stops** — exits driven by: stop loss, momentum stall, thesis break, trailing stop, or earnings proximity
- **Learning feedback loop** — closed trades feed mistake patterns that adjust future entry scoring

---

## Architecture

### Capital Allocation (Unified $5,000 Paper Pool)

| Track | Module | Allocation | Sizing |
|---|---|---|---|
| **Long-Term** | `core/paper_trading.py` | $3,500 | Conviction-weighted: 8.0 conviction → 5.5% of total value, 10.0 → 7% (interpolated within tiers) |
| **Swing/Speculative** | `core/growth_paper_trading.py` | $1,500 | Base slot = total_value / 6, scaled by setup quality (4+/7 signals) and risk/reward ratio |

Both compound independently. Exit proceeds return to cash pool; next entries size off live portfolio value.

### 7-Agent Deep Pipeline

```
Hunter → Critic → Sentiment → Validator → Portfolio Manager + Scout + Journal
```

| Agent | Role | Output |
|---|---|---|
| **Hunter** | Scores fundamental + technical + valuation strength (0-10) | Quantitative signal |
| **Critic** | Red flags: litigation, SEC issues, auditor problems | Kill switches |
| **Sentiment** | BigData.com sentiment + narrative momentum | ±1.5 conviction boost/penalty |
| **Validator** | Combines all agents → BUY / WATCHLIST / AVOID | Final signal |
| **Portfolio Manager** | Conviction-weighted sizing, concentration limits, anti-whipsaw | Position recommendation |
| **Scout** | Weekly sector funnel (momentum-ranked) | Universe sub-selection |
| **Journal** | Trade logging, drift detection, outcome tracking | Performance attribution |

### Signal Logic (Long-Term)

```
BUY      → conviction >= 8 AND data_confidence >= 7
WATCHLIST → conviction 6-7 AND data_confidence >= 6
AVOID    → anything else (or conviction capped by macro/valuation gates)
```

**Gates & Penalties:**
- **Valuation**: OVERVALUED (MOS < 0%) caps conviction at 7.0
- **Macro**: 2+ headwinds (yield spike, credit stress, high VIX) → −0.5 to −1.5 conviction
- **Sector**: Lagging sector → BUY blocked, WATCHLIST → AVOID
- **Correlation**: Position would push sector exposure >30% → WATCHLIST
- **Earnings**: Entry blackout <10 days before print
- **Conviction trend**: −0.3 if trending down >1.5 over 30 days
- **Mistake patterns**: Learned from closed losses (e.g., "entering at RSI>68") → −0.5 to −1.0

### Swing Entry Pipeline (Three-Tier Funnel)

```
~450 tickers (S&P500 + Nasdaq100)
    ↓ momentum pre-filter (top 150 by 60d return)
    ↓ 7 numerical signals (volume, strength, structure, catalyst, narrative, insider, squeeze)
    ↓ chart vision (Claude Vision on 4+/7 candidates)
    ↓ auto-entry gates (R/R ≥2.0, price in entry zone, no earnings blackout)
~5-15 positions
```

**7 Signals:**
| Signal | Fires When |
|---|---|
| volume_accumulation | 20d vol ≥1.2x 90d avg |
| relative_strength | outperforming sector ETF & SPY |
| price_structure | Stage 2, near 52-week high |
| catalyst_proximity | earnings 14-35d out (needs +2 other signals) |
| narrative_momentum | ≥75% beat rate, >5% surprise, >10% YoY revenue |
| insider_buying | net bullish or CEO/CFO buying |
| short_squeeze | short float ≥10%, RSI>50, Stage 2 |

---

## Daily Automated Schedule

**Managed via Claude Code's `mcp__scheduled-tasks`** (runs while app is open)

| Task | Time | What It Does |
|---|---|---|
| `equitylens-universe-refresh` | Sun 7 AM | Live scrape S&P500 + Nasdaq100, liquidity filter, rebuild universe cache |
| `equitylens-weekly-review` | Sun 8 AM | BigData refresh, sector funnel, outcome review, SPY baseline |
| `equitylens-daily-scan` | 9:35 AM (Mon-Fri) | Deep pipeline on 60-80 weekly universe stocks, swing scan on full 450, auto-exits, stop re-eval |
| `equitylens-decision-capture` | 4:30 PM (Mon-Fri) | "Did you invest?" prompt for manual verification of BUY signals |
| `equitylens-paper-report` | 5 PM (Mon-Fri) | Evening P&L summary |

**Dashboard:** `dashboard/app.py` (Uvicorn FastAPI server, runs on port 8000)

---

## Key Data Files

```
data/
  daily_scan_2026-07-07.json          Full scan output (65+ deep results, swing candidates)
  swing_candidates_2026-07-07.json    Charted swing setups ready for entry
  paper_portfolio.json                LT paper holdings ($3.5k allocation)
  growth_portfolio.json               Swing paper holdings ($1.5k allocation)
  swing_positions.json                Manual swing position tracking
  longterm_positions.json             Manual LT position tracking
  screen_performance.json             Feedback loop: signal outcomes for each screen
  mistake_log.json                    Learned patterns from closed losses
  signal_outcomes.jsonl               Historical signal-to-30/90d-return tracking
  swing_charts/{ticker}_{date}.png    Daily swing chart (120d OHLCV)
  swing_charts/{ticker}_LT_{date}.png Weekly LT chart (1y OHLCV)
  pnl_history_*.json                  Daily portfolio snapshots (P&L tracking)
  bigdata_cache/{ticker}.json         BigData.com sentiment & news (refreshed Sunday)
  universe_cache.json                 Live S&P500 + Nasdaq100 constituents (7d TTL)
  ohlcv_cache_{date}.parquet          Batch OHLCV for prefilter (one yfinance call)
  weekly_universe_{date}.json         Sector funnel output (deep scan universe)
```

---

## Dashboard Tabs

| Tab | Shows | Source |
|---|---|---|
| **Signals → Long-Term** | BUY/WATCHLIST/AVOID results | `/api/scan` |
| **Signals → Swing/Spec** | 7-signal candidates with chart analysis | `/api/swing/candidates` |
| **Swing** | Open swing positions + exit alerts | `/api/swing/positions` + `/api/swing/candidates` |
| **Long-Term** | Open LT positions + conviction | `/api/longterm/positions` + `/api/paper/portfolio` |
| **Portfolio** | All positions (unified), track breakdown, P&L | `/api/capital/overview` |
| **Closed Trades** | Realized trade history (entry, exit, P&L, reason) | `/api/trades/closed` |
| **Decisions** | Daily activity log (BUY signals, exits, errors) | `/api/decisions/daily-log` |
| **Weekly Review** | Sector funnel, outcome review, missed opportunities | `/api/scan/weekly` + `/api/review/weekly` |

---

## Running Locally

### Prerequisites
- Python 3.10+
- Virtual environment (`.venv`)
- Dependencies: `pip install -r requirements.txt`

### Setup
```bash
cd /Users/sohampatil/Documents/Projects/equitylens

# Create virtual environment (if not already present)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Start Dashboard
```bash
PYTHONPATH=. .venv/bin/python -m uvicorn dashboard.app:app --reload --host 127.0.0.1 --port 8000
```
Then open: http://127.0.0.1:8000

### Manual Workflows
```bash
# Run today's daily scan (full pipeline)
PYTHONPATH=. .venv/bin/python workflows/daily_scan.py

# Weekly sector funnel
PYTHONPATH=. .venv/bin/python workflows/weekly_scan.py

# Swing scan only (full universe prefilter)
PYTHONPATH=. .venv/bin/python workflows/run_swing_scan.py

# Weekly outcome review
PYTHONPATH=. .venv/bin/python workflows/outcome_review.py

# Backtest signal outcomes
PYTHONPATH=. .venv/bin/python workflows/run_backtest.py

# Force universe rebuild (ignore 7-day cache)
PYTHONPATH=. .venv/bin/python -c "from core.universe import build_universe; build_universe(force_refresh=True)"
```

---

## Key Insights & Design Decisions

### Stop Loss Formula (Both Tracks)
```
stop = max(S1 − 0.5×ATR, entry − 2.5×ATR)
```
- **S1** = nearest tested support (5-bar swing lows, 1.5% clustering)
- **0.5×ATR buffer** = survives stop hunts / liquidity sweeps
- **2.5×ATR floor** = caps risk when nearest support is far below

Weekly re-evaluation: if a new support forms above current stop, raise it (never lower).

### No Parallel Pipelines
Swing and speculative entries flow through **one unified pipeline** (`daily_scan.py`). Both the 7-signal screener and Growth Hunter scoring convert results to `SwingSignal` objects → same chart-vision gate → same auto-entry authority. This ensures:
- Consistent entry rules regardless of signal source
- No bypass of earnings blackout or risk caps
- Single feedback loop for learning

### Feedback Loop (Learning from Losses)
- At signal time: record entry price, screens matched, Hunter score, RSI, chart pattern
- At exit time: record exit price, reason, P&L, hold days → outcome (WIN/LOSS/SCRATCH)
- Weekly: scan for patterns (high RSI entries, low-score swings, weak chart confidence, single-screen confluence)
- Active: mistake patterns penalize new candidates matching lost-trade conditions (e.g., "−0.5 conviction if Hunter<5.5 on swings")

### Why Conviction Caps Don't Auto-Demote Signals (Fixed July 7)
Valuation, macro, and mistake-pattern gates cap conviction (e.g., OVERVALUED → 7.0). This does NOT automatically flip the signal to WATCHLIST — you must **re-check the threshold** after every adjustment. Otherwise OVERVALUED stocks (MOS −100%) stay BUY, macro headwinds are ignored, and learned loss patterns don't prevent re-entry.

---

## Known Gotchas

- **yfinance rate limits**: Never fetch per-ticker in a loop — use one batch `yf.download(list, ...)`. The parquet cache exists for this.
- **yfinance `.calendar` format**: Returns a dict on current versions; earnings dates are `datetime.date` objects (no `.date()` method needed).
- **Split risk**: Position stores raw shares/entry; unhandled splits read as crashes. Split guards check and adjust.
- **BigData cache refresh**: Runs Sunday only. Daily scans read from cache files (`data/bigdata_cache/{ticker}.json`).
- **Dashboard restart**: After editing core/*.py, restart the dashboard (`pkill -f "uvicorn dashboard.app"`). Hard-refresh browser (Cmd+Shift+R).

---

## Architecture Overview

```
workflows/
  daily_scan.py          Main entry point (9:35 AM, Mon-Fri)
  weekly_scan.py         Sector funnel (Sunday 8 AM)
  outcome_review.py      Feedback loop review (Sunday)
  run_backtest.py        Historical validation

core/
  orchestrator.py        Chain all 7 agents, apply gates
  hunter.py              Fundamental + technical scoring (0-10)
  critic.py              Red flags & kill switches
  sentiment.py           BigData sentiment + narrative
  validator.py           Signal consolidation
  portfolio_manager.py   Sizing logic
  scout.py               Sector funnel
  data_layer.py          yfinance, BigData, price/volume/FCF
  stop_loss.py           Stop tier calculation & re-eval
  conviction.py          Conviction formula + trend tracking
  feedback.py            Signal outcomes, mistake patterns, learning
  position_store.py      Unified position tracking
  paper_trading.py       LT portfolio ($3.5k), auto-execute BUYs & stops
  growth_paper_trading.py Swing portfolio ($1.5k), trailing stops, trims
  swing_chart_analysis.py Chart vision pipeline (Claude Vision)
  universe.py            S&P500/Nasdaq100 constituent fetching
  sector_map.py          Sector definitions & micro-sectors
  screener.py            7 numerical signals + adaptive sizing
  valuation.py           Graham formula + sector-PE fair value
  macro_pulse.py         10Y yield, DXY, credit spread overlay
  earnings_calendar.py   Earnings dates & blackout logic
  correlation.py         Sector concentration limits
  bias_check.py          Behavioral bias detection
  
agents/
  hunter.py, critic.py, sentiment.py, validator.py, portfolio_manager.py, scout.py, journal.py

dashboard/
  app.py                 FastAPI server (Uvicorn)
  templates/index.html   React-style dashboard UI
  static/                CSS/JS assets
```

---

## Trading Philosophy

> **Discipline beats intuition; process beats prediction.**

EquityLens does not predict market direction. It:
1. Identifies setups that align with technical + fundamental + sentiment confluence
2. Sizes positions based on conviction, not conviction — smaller when signal is weaker
3. Lets stops enforce risk management — never holds a bad setup on hope
4. Learns from losses — patterns that trigger repeat losses penalize similar future setups
5. Automates routine execution — so the human can focus on the exceptions

---

## Future Enhancements

- Real-time alerts for stop/limit fills
- Integration with live brokers (Alpaca, Interactive Brokers)
- Multi-timeframe confirmation (daily + weekly alignment)
- Sector rotation automation (rebalance weights vs SPY)
- Advanced options strategies for income on held positions

---

## Support & Questions

For issues, improvements, or questions, please open an issue on GitHub or refer to the inline code documentation in `CLAUDE.md`.

---

**Last Updated:** July 7, 2026  
**Author:** Soham Patil  
**License:** MIT
