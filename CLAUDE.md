# EquityLens — AI Equity Research Platform

## Philosophy
Paper portfolios are **fully automated** (entries + exits) on both tracks. Real trades are executed manually by the user after reviewing signals. Goal: build a precise track record in paper mode, then act on it with confidence.

- Adaptive conviction-based sizing on both tracks — no fixed dollar positions
- Capital compounds: exit proceeds recycle into the pool; slot sizes grow after wins, shrink after losses
- No fixed price targets, no time stops
- Exits driven by: stop_loss | momentum_stall | thesis_break | trail_stop | earnings_proximity (decision prompt)
- Sentiment cache refresh (yfinance + Haiku, no paid BigData): **Sunday only** — daily scans read from weekly cache

---

## Capital — one unified $5,000 paper pool

| Track | Module | Capital | Sizing |
|---|---|---|---|
| Long-term | `core/paper_trading.py` | $3,500 | Continuous conviction-weighted: conviction 8.0 → 5.5%, 10.0 → 7% of total value (interpolated within tiers; 7–8 → 3–4%; below → 2%) |
| Swing | `core/growth_paper_trading.py` | $1,500 | Base slot = total_value / 6, scaled by setup quality: `0.35 + 0.6×(score/7) + 0.4×min(RR,4)/4`, capped 1.25x slot, floor ~$100 |

Both compound independently. Exit proceeds return to cash (`p.cash += proceeds` on all exits/trims); next entries size off live total value.

**Swing auto-entry criteria:** chart entry_type in breakout/pullback/bounce (not "wait") always required. Selection gates are ADAPTIVE (`settings.swing_entry_mode = "exploration"`, since 2026-07-08): each gate runs loose (3+/7 signals, R/R ≥ 1.2, zone +5%) until its own cohort evidence tightens it back to strict (4+/7, R/R ≥ 2.0, zone +2%). Entries failing any strict gate are tagged `strict:*` in their feedback record and sized 0.5x (probation). `feedback.adapt_swing_gates()` (called at the start of every auto-entry pass) self-tightens a gate when its cohort has ≥10 closed trades and hit rate <45% or avg return 3pts below the clean cohort — one-way ratchet, state in `data/swing_gate_state.json` (delete to reset loose), decisions logged in its `history`. Scoreboard: `/api/feedback/gates`. Risk controls (chart stop, 1.5% risk cap, earnings blackout, sector slots, cross-track dedup) are NEVER loosened. Implemented in `growth_paper_trading.auto_enter_swing_signals()`, called by daily_scan.

**Long-term auto-entry:** every BUY signal from the orchestrator → `paper_trading.auto_execute_scan_signals()`. Paused during VIX spikes.

---

## Stop-Loss Formula (both tracks)

```
stop = max(S1 − 0.5×ATR,  entry − 2.5×ATR)
```

- S1 = nearest tested support below price (from `_find_sr_levels`: 5-bar swing lows, 1.5% clustering, ≥2 tests)
- 0.5×ATR buffer below support survives stop hunts / liquidity sweeps
- 2.5×ATR floor caps risk when nearest support is far below (near-52w-high setups)
- Swing: daily bars, 120d lookback. Long-term: weekly bars, 1y lookback (naturally wider)
- Legacy 3-tier ATR system (`core/stop_loss.py`) still computes tier1/2/3; chart stop overrides tier1 when tighter
- `stop_loss.reevaluate_stop()`: weekly re-check on held positions — if a new S1 formed above the current stop, raise it (never lower, never above entry). Runs at end of daily scan for both portfolios.

---

## Architecture

### Core pipeline (7 agents)
```
Hunter → Critic → Sentiment → Validator → Portfolio Manager + Scout + Journal
```

```
core/
  data_layer.py          yfinance: price, ATR, RSI, MA50/200, volume, FCF, D/E
                         Also fetches: volume_avg_90d, short_float_pct
  bigdata_client.py      Parse sentiment cache → internal types (cache now written by
                         yfinance_sentiment.py — BigData MCP replaced June 2026)
  staleness.py           GREEN/YELLOW/RED/BLACK data freshness
  regime_detector.py     3-signal market regime (SPY/VIX/drawdown) → BULL/NEUTRAL/BEARISH
  conviction.py          3-gate conviction formula with kill switch
  stop_loss.py           3-tier ATR stops + reevaluate_stop() weekly S/R-based stop raiser
  orchestrator.py        Chains all agents; runs LT chart vision on BUYs; valuation gate;
                         correlation check; macro penalty. AnalysisResult carries lt_chart,
                         fair_value_low/high, margin_of_safety, macro_headwinds
  persistence.py         Conviction history + portfolio positions (local JSON + Supabase sync)
  conviction_monitor.py  Conviction drop response matrix — wired into run_batch
  earnings_calendar.py   Earnings gate: NORMAL/WATCH/CAUTION/BLACKOUT.
                         get_next_earnings() handles yfinance dict AND DataFrame formats
                         (dict values are datetime.date — do not assume .date() exists)
  backtest.py            Signal-replay + historical-scan + SPY baseline comparison
  correlation.py         Correlation clusters + entry_correlation_check(): blocks BUY→WATCHLIST
                         if adding position pushes sector exposure past 30%
  universe.py            LIVE Wikipedia scrape of S&P 500 (503) + Nasdaq 100 (~101) via
                         requests+pandas (verify=False for local SSL issue), hardcoded fallback.
                         Liquidity filter → ~450 tickers cached 7d in data/universe_cache.json
                         (format: {fetched_date, count, entries: [{ticker, sector}]})
  bias_check.py          Behavioral bias checkpoints: FOMO, recency, loss aversion, overconfidence, anchoring
  sector_map.py          Single source of truth: 10 macro sectors, 24 microsectors, 14 wildcards
  screener.py            Swing prefilter + 7-signal screener + adaptive sizing (see Swing section)
  momentum_monitor.py    Daily exit checker for open swings — position_store AND growth portfolio
                         positions. Checks: stop, stall, thesis break, earnings ≤5d (decision prompt)
  position_store.py      SwingPosition + LongTermPosition dataclasses, JSON persistence
                         Also: capital_overview() — unified view across all position sources
  paper_trading.py       Long-term paper portfolio ($3,500): auto-enter BUYs, auto-exit stops/trims
  growth_paper_trading.py Swing paper portfolio ($1,500): auto_enter_swing_signals(), compounding
                         slot sizing, trailing stops, trims at +150%/+300%
  swing_chart_analysis.py Chart pipeline: OHLCV fetch (parquet cache first) → S/R detection →
                         3-panel dark chart render → Claude Vision → SwingChartSignal.
                         analyze_swing_candidate() = daily/120d; analyze_longterm_candidate() =
                         weekly/1y, saves {ticker}_LT_{date}.png (suffix avoids clobbering swing chart).
                         Vision prompt includes EXACT volume numbers (5/20/90d avgs, up-day vs
                         down-day volume) — model reasons numerically, chart is for structure only
  valuation.py           Intrinsic value gate: Graham formula (EPS × (8.5 + 2×growth)) or sector-PE
                         fallback → UNDERVALUED/FAIR/OVERVALUED. OVERVALUED caps conviction at 7.0
  signal_tracker.py      Feedback loop: record_signal() → data/signal_outcomes.jsonl;
                         update_outcomes() scores 30/90d returns; get_adaptive_weights() adjusts
                         Hunter's 50/30/20 weights by 90d hit rates (bounds 0.10–0.65)
  macro_pulse.py         Macro overlay: 10Y yield (^TNX), DXY, HYG/LQD credit spread, Fed calendar.
                         2+ headwinds → conviction penalty 0.5–1.5. Cached data/macro_pulse_{date}.json
  chart_refresh.py       Re-runs vision on swing candidates >3d old; marks STALE if entry→wait
                         or R/R <1.5. Called after swing scan in daily_scan
  chart_renderer.py      (legacy) candlestick PNG via mplfinance for chart_vision agent
  vision_cache.py        Smart invalidation cache for chart_vision agent (weekly/daily hunter charts)

agents/
  hunter.py              Score 0-10. Weights ADAPTIVE via signal_tracker.get_adaptive_weights()
                         (base: fundamentals 50% + technicals 30% + valuation 20%)
  critic.py              Red flags + kill switches (litigation, SEC, auditor)
  sentiment.py           BigData sentiment + score_sentiment_dimensions(): earnings_quality /
                         narrative_momentum / institutional_signal (-1..+1 each, 40/30/30 composite)
  validator.py           Combines all agents → BUY/WATCHLIST/AVOID
  portfolio_manager.py   CONTINUOUS conviction-weighted sizing (see Capital), concentration limits,
                         anti-whipsaw
  scout.py               Sector funnel: macro sectors scored vs SPY, microsector drill-down
                         run_weekly_funnel() → WeeklyUniverse (used by weekly_scan.py)
  chart_vision.py        Hunter-score chart adjustment (weekly ≥6, daily ≥8, 40/60 combine)
  journal.py             Trade logging + drift detection

workflows/
  daily_scan.py          9:35 AM Mon-Fri:
                           1. update_outcomes() — score pending signal outcomes
                           2. Deep 7-agent pipeline on weekly universe (~60-80 stocks);
                              BUYs get LT chart vision + valuation gate + macro penalty
                           3. record_signal() for every BUY (feedback loop)
                           4. paper_trading.daily_update() → auto-exit; auto_execute_scan_signals() → auto-enter
                           5. Swing scan: swing_universe_prefilter(150) on full ~450 universe →
                              7-signal pass → chart vision on 4+/7 → auto_enter_swing_signals()
                           6. monitor_open_swings() → exit alerts (incl. growth positions + earnings gate)
                           7. reevaluate_stop() on all held positions (both portfolios)
                           8. refresh_stale_candidates() — re-vision charts >3d old
                           9. Bias sweep + baseline comparison
                         Saves daily_scan_{date}.json (results include lt_chart dict) +
                         swing_candidates_{date}.json
  weekly_scan.py         Sunday: run_weekly_funnel() → saves data/weekly_universe_{date}.json
  outcome_review.py      Sunday: outcome review + missed opportunities + behavioral bias report
  bigdata_refresh.py     MCP refresh stub — actual refresh happens via Claude in Sunday task
  run_swing_scan.py      Standalone swing scan — uses swing_universe_prefilter (NOT weekly universe),
                         falls back to weekly universe if prefilter empty
  decision_capture.py    4:30 PM: "did you invest?" for each BUY signal
  refresh_universe.py    Universe rebuild (respects 7d cache; force via build_universe(force_refresh=True))
```

---

## Swing Pipeline (three-tier funnel)

```
~450 stocks (live S&P500 + Nasdaq100, universe_cache)
  ↓ swing_universe_prefilter — ONE yf.download batch call, cached to
  ↓ data/ohlcv_cache_{date}.parquet (charts + S/R reuse it — no per-ticker refetch)
  ↓ filters: price $10–250 | avg vol >1M shares | RSI 35–75 | price >MA50×0.97 | ATR 1.5–6% of price
~150 stocks
  ↓ 7-signal check (numerical, no LLM)
candidates with ≥2 signals (~60-100 in earnings season)
  ↓ chart vision — ONLY 4+/7 signal stocks (cost control)
~5-15 charted → auto-entry if criteria met (see Capital)
```

### 7 signals (`core/screener.py`)
| Signal | Fires when | Notes |
|---|---|---|
| volume_accumulation | 20d vol ≥1.2x 90d avg | numerical |
| relative_strength | outperforming sector ETF / SPY | |
| price_structure | Stage 2, near 52w high | |
| catalyst_proximity | earnings 14–35d away **AND** RS/structure/volume also fired | date alone = dropped (context rule in scan loop) |
| narrative_momentum | beat rate ≥75% AND surprise >5% AND revenue >10% YoY | recalibrated — beats alone fire for everyone |
| insider_buying | net_insider_signal BULLISH or ceo_cfo_buying | rare in mega-caps by nature |
| short_squeeze | short float ≥10% + RSI>50 + Stage 2 | rare — squeezed stocks rarely pass prefilter |

Conviction: 5+/7 HIGH, 3–4 MEDIUM, 2 LOW. HIGH is intentionally rare (needs insider/squeeze on top of the common four).

### Growth Hunter — second candidate source, same entry path

`agents/growth_hunter.py` scores `core/growth_universe.py`'s curated small/mid-cap
list ($300M–$10B, excluded from the main S&P500/Nasdaq100 universe) on a
different, fundamentals-first rubric (sector tailwind 30% / revenue quality
25% / margins 20% / technicals 15% / moat 10%; Rule of 40 displayed but not
scored). `SPECULATIVE BUY` (score ≥7.0/10) results are converted to
`SwingSignal` objects by `core.screener.growth_hunter_candidates()` and
merged into the same `swing_signals` list the 7-signal screener produces, in
`daily_scan.py`. Both sources get the same chart-vision confirmation and flow
through the same `auto_enter_swing_signals()` gates (chart-confirmed entry,
R/R ≥2.0, earnings blackout, cross-track dedup, sector slots, risk-based
sizing) — one portfolio, one schedule, one set of rules regardless of which
scorer sourced the idea.

Until 2026-07-05 this ran as its own script (`workflows/growth_scan.py`) on
its own 9:40 AM schedule, calling `execute_buy()` directly and skipping every
gate above — that script is now a deprecated no-op stub; don't resurrect it
as a separate entry point.

---

## Signal Logic (long-term)

- **BUY**: conviction >= 8 AND data_confidence >= 7
- **WATCHLIST**: conviction 6–7 AND data_confidence >= 6
- **AVOID**: anything else
- **KILL SWITCH**: litigation / SEC / auditor → conviction = 0, no exceptions
- **Valuation gate**: OVERVALUED (price >10% above Graham/sector-PE fair value) caps conviction at 7.0
- **Correlation gate**: BUY → WATCHLIST if position would push sector exposure past 30%
- **Macro penalty**: 2+ macro headwinds subtract 0.5–1.5 conviction from BUYs
- **LT chart on BUY**: analyze_longterm_candidate() runs on every BUY — entry zone, weekly S/R stop, alert line. Chart's job here = entry timing + stop placement; the R/R gate applies only to swing (LT targets are just first weekly resistance, not the thesis)

---

## Sector Map (core/sector_map.py)

10 macro sectors → 24 microsectors + 14 wildcards. Used by the weekly funnel (scout.py).
Sector scoring vs SPY: composite = 50% relative_return_60d + 30% momentum_accel + 20% volume_breadth
→ LEADING/MIDDLE/LAGGING → top 3 macro → microsectors → ~60 weekly candidates.
Note: the weekly funnel constrains the DEEP scan only; the swing scan always runs on the full universe.

**Wildcard pool:** COST, TSLA, AMZN, BRK-B, AAPL, MELI, CELH, AXON, CAVA, DUOL, APP, NFLX, UBER, COIN

---

## Automated Schedule

Managed via Claude Code's own scheduler (`mcp__scheduled-tasks`), not system
cron/launchd — these only fire while the Claude Code app is open (or on next
launch if it was closed). List/inspect with `list_scheduled_tasks`; each
task's prompt lives at `~/.claude/scheduled-tasks/{taskId}/SKILL.md`.

| Task | Schedule | What it does |
|---|---|---|
| equitylens-universe-refresh | Sunday 7 AM | Live-scrapes S&P 500 + Nasdaq 100, liquidity filter, rebuilds universe_cache.json |
| equitylens-growth-universe-refresh | Sunday 7:30 AM | Validates the curated growth_universe.py list, sector radar, flags additions/removals |
| equitylens-weekly-review | Sunday 8 AM | Sentiment cache refresh (yfinance + Haiku) → sector funnel → outcome review → SPY baseline → Monday briefing |
| equitylens-daily-scan | 9:35 AM Mon-Fri | Full pipeline incl. Growth Hunter (see workflows/daily_scan.py above) |
| equitylens-decision-capture | 4:30 PM Mon-Fri | "Did you invest?" for each BUY signal |
| equitylens-paper-report | 5 PM Mon-Fri | Evening P&L summary (workflows/paper_trade_report.py) |

`equitylens-growth-scan` (9:40 AM Mon-Fri) is **disabled** as of 2026-07-05 —
see the Growth Hunter note in the Swing Pipeline section below.

---

## Sentiment Cache (formerly BigData.com MCP)

The paid BigData.com MCP subscription was **replaced June 2026** by
`core/yfinance_sentiment.py` + `workflows/bigdata_refresh.py`:
yfinance (headlines, analyst consensus/targets, EPS surprises, insider Form 4s)
scored by Claude Haiku (~$0.03/week) → sentiment score, risk flags
(litigation/SEC/auditor), narrative summary. Do NOT call BigData MCP tools.

**Refresh schedule:** Sunday only (equitylens-weekly-review task, Step 1 runs
`workflows/bigdata_refresh.py`; check coverage with `--status`, refresh specific
tickers by passing them as args). Daily scans read from
`data/bigdata_cache/{ticker}.json` — no fetching at scan time.
Same cache format as before (`source: "yfinance+haiku"`), so
`bigdata_client.py` and all agents work unchanged.

---

## Dashboard Tabs

| Tab | Source APIs | What it shows |
|---|---|---|
| Signals → Long-Term | /api/scan | BUY/WATCHLIST/AVOID signals |
| Signals → Swing/Spec | /api/swing/candidates | 7-signal candidates with chart fields |
| Swing | /api/swing/positions + /api/swing/candidates | Positions + exit alerts + candidates (rows → chart modal) |
| Long-Term | /api/longterm/positions + /api/paper/portfolio | LT + AUTO paper holdings — **rows clickable → chart modal** |
| Portfolio | /api/capital/overview + position APIs | Unified positions table — **rows clickable → chart modal** |
| Decisions | /api/decisions/daily-log | Date-wise activity log, collapsible day cards (today expanded) |
| Weekly Review | /api/scan/weekly + /api/review/weekly | Sector funnel + outcome review |

### Chart endpoints (dashboard/app.py)
- `GET /api/swing/chart/{ticker}` — analysis JSON. Lookup order: today's swing_candidates →
  daily scan results' `lt_chart` (returns `timeframe: "weekly (1y) — long-term"` and a
  `chart_url` with `?variant=lt`) → fresh analysis
- `GET /api/swing/chart/{ticker}/image?variant=lt` — serves `{ticker}_LT_{date}.png` (weekly);
  no variant = daily swing chart; falls back to the other variant, then most recent

---

## Running

```bash
# Always use venv AND PYTHONPATH (workflows have no sys.path bootstrap except run_swing_scan)
PYTHONPATH=/Users/sohampatil/Documents/Projects/equitylens .venv/bin/python ...

# Start dashboard
PYTHONPATH=. .venv/bin/python -m uvicorn dashboard.app:app --reload --host 127.0.0.1 --port 8000

# Manual daily scan
PYTHONPATH=. .venv/bin/python workflows/daily_scan.py

# Manual swing scan (full-universe prefilter; saves data/swing_candidates_{date}.json)
PYTHONPATH=. .venv/bin/python workflows/run_swing_scan.py

# Manual weekly sector funnel
PYTHONPATH=. .venv/bin/python workflows/weekly_scan.py

# Force universe rebuild (ignores 7d cache)
PYTHONPATH=. .venv/bin/python -c "from core.universe import build_universe; build_universe(force_refresh=True)"

# Weekly outcome review / backtest
PYTHONPATH=. .venv/bin/python workflows/outcome_review.py
PYTHONPATH=. .venv/bin/python workflows/run_backtest.py
```

---

## Key Data Files

```
data/
  weekly_universe_{date}.json    Sunday sector funnel output — deep scan reads this all week
  universe_cache.json            Live S&P500+Nasdaq100 (~450 tickers), 7d TTL, Sunday 7AM refresh
  ohlcv_cache_{date}.parquet     Daily batch OHLCV (90d × ~450 tickers) — prefilter writes,
                                 charts/S-R/signals read. One yfinance call per day
  swing_candidates_{date}.json   Swing scan output — dashboard Swing tab reads this
  daily_scan_{date}.json         Full scan results; BUY results include lt_chart dict
  swing_charts/{t}_{date}.png    Daily swing chart (120d)
  swing_charts/{t}_LT_{date}.png Weekly long-term chart (1y) — separate file, no clobbering
  paper_portfolio.json           Long-term paper portfolio ($3,500 start)
  growth_portfolio.json          Swing paper portfolio ($1,500 start)
  signal_outcomes.jsonl          Feedback loop: every BUY/WATCHLIST + 30/90d outcomes
  macro_pulse_{date}.json        Daily macro overlay cache
  swing_positions.json           position_store swing positions (manual/decision_capture)
  longterm_positions.json        position_store long-term positions
  weekly_review_{date}.json      Sunday outcome review output
  bigdata_cache/{ticker}.json    BigData MCP cache — refreshed Sunday, read all week
```

---

## Known Gotchas

- **yfinance rate limits**: never fetch per-ticker in a loop over the universe — use one
  `yf.download(list, ...)` batch call. The parquet cache exists for exactly this reason.
- **yfinance `.calendar`** returns a dict on current versions (older: DataFrame). Earnings dates
  in the dict are plain `datetime.date` objects.
- **Silent `except: pass` blocks** have twice hidden dead signals (catalyst, insider). When a
  signal never fires across a full scan, suspect a swallowed exception, not the market.
- **Wikipedia scrape** needs `requests` with `verify=False` on this machine (local SSL cert issue)
  and `pd.read_html(StringIO(r.text))`.
- **DEBUG logging changes yfinance behavior** (disables threaded batch download) — don't debug
  batch issues with logging.DEBUG on.
- Dashboard restart after backend changes: `pkill -f "uvicorn dashboard.app"` then restart with
  PYTHONPATH set. Hard-refresh browser (Cmd+Shift+R) after HTML/JS changes.
