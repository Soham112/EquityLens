# EquityLens — Self-Improving AI Equity Research Platform

An AI-driven equity research and paper-trading platform that **learns from its own
track record**. EquityLens combines fundamental analysis, technical chart vision
(Claude), and sentiment data to trade two automated paper portfolios — and every
rule it trades by is treated as a hypothesis: backtested before it ships, tagged
and measured while it runs, and tightened or replaced when its own evidence says so.

> **EXPERIMENTS.md** is the heart of the project: a living log of every behavioral
> change — hypothesis, pre-registered success measure, evidence, verdict. Nothing
> changes trading logic without an entry there.

## Philosophy

Paper portfolios are **fully automated** (entries + exits) on both tracks. Real
trades are executed manually by the user after reviewing signals. Goal: build a
precise, honest track record in paper mode, then act on it with confidence.

- **Backtest what is mechanically replayable; forward-observe only what depends on
  live judgment.** Never forward-wait for what history can answer today.
- **Make mistakes cheap, labeled, and counted.** Losses are tuition — but only if
  every trade carries tags explaining which rule allowed it.
- Adaptive conviction-based sizing, compounding capital, no fixed targets or time
  stops. Risk controls (stops, risk caps, blackouts) are never loosened; selection
  rules are hypotheses under test.

## Capital — one unified $5,000 paper pool

| Track | Capital | Sizing |
|---|---|---|
| Long-term (`core/paper_trading.py`) | $3,500 | Conviction-weighted: 8.0 → 5.5%, 10.0 → 7% of total value |
| Swing (`core/growth_paper_trading.py`) | $1,500 | Slot = total/6 × setup quality; **0.5x probation** for entries passing loose-but-not-strict gates |

Both compound independently; exit proceeds recycle into their pool.

## The learning loops (what makes it self-improving)

| Loop | What it does | Where to see it |
|---|---|---|
| **E7 — Adaptive swing gates** | Entry gates run loosened (3+/7 signals, R/R ≥1.2); entries failing strict gates are tagged `strict:*` and half-sized; each gate **self-tightens** when its cohort proves bad (≥10 closed, hit <45%) | `/api/feedback/gates` |
| **E8 — Shadow tracking (LT)** | Every gate that demotes a BUY stamps its name (`demoted_by`); demoted stocks are scored at 30/90d as **shadow trades** — measuring the road not taken at zero risk | Long-Term tab → 👁 Shadow Tracking |
| **E9 — Sector formula race** | The funnel's ranking formula was backtested over 5y AND 27y; the live Sunday race logs every challenger's ranking + forward returns until one earns production | Weekly Review → 📊 5-Year Sector Backtest |
| **E10 — Accel radar + probe** | Sectors accelerating outside the funnel's top-5 get flagged; their strongest microsector's stocks audition in the deep scan (tagged `source=radar`) | Weekly Review funnel ⚡ callout |
| **E13 — Vision grading** | Every chart-vision verdict from daily scans is graded against what prices did next — the analyst gets a hit-rate, free | Sunday review, EXPERIMENTS.md |
| **E15 — Super-Performer discovery** | Weekly Minervini Trend Template screen over ~1,000 S&P 400/600 mid/small caps → research shortlist → one-time company dossiers → admits face the same gates (tagged `source:discovery`) | Super Performers ★ tab |
| **Mistake patterns** | Recurring loss conditions (evidence-gated) penalize matching new candidates, bounded −1.5 conviction | `/api/feedback/mistakes` |
| **Signal outcomes** | Every BUY/WATCHLIST scored at 30/90d with simulated stops; Hunter's weights adapt to 90d hit rates | `/api/feedback/summary` |

## Architecture

### Core pipeline (7 agents)
```
Hunter → Critic → Sentiment → Validator → Portfolio Manager + Scout + Journal
```

### Long-term signal logic
- **BUY**: conviction ≥8 AND data confidence ≥7 — every BUY gets weekly-chart vision
- Gates that demote (all recorded for shadow tracking): valuation (Graham/sector-PE
  cap at 7.0), macro headwinds (−0.5 to −1.5), sector momentum, correlation (>30%
  sector exposure), earnings blackout, VIX pause, conviction trend, mistake patterns
- **Every conviction adjustment re-checks the signal** — a capped 7.0 can never
  ship as a BUY (E4, learned the hard way)

### Swing pipeline (three-tier funnel)
```
~458 stocks (live S&P 500 + Nasdaq 100)
  ↓ math prefilter (price/volume/RSI/MA/ATR) — one batch download, 400d cache
  ↓ 7 signals — incl. price_structure = full Minervini Trend Template
    (MA50>150>200 rising, ≥30% above 52w low, ≤25% off high, RS percentile ≥70 —
     E14: beat the old check 11/11 years at both 21d and 63d horizons)
  ↓ chart vision (Claude/Sonnet) on 3+/7 — 18-pattern vocabulary, anti-bias rules,
    MA200 + 52-week-high context on every chart
  ↓ adaptive gates + probation sizing → entry
```

### Stops (E11/E12 — backtested on 4,000 entries, era-split stable)
```
Swing:      stop = S1 − 0.5×ATR      (2.5×ATR fallback only when no tested support)
Long-term:  stop = max(S1 − 0.5×ATR, entry − 2.5×ATR)
```
S1 = nearest support tested ≥2×. Trailing stops, profit trims, and the momentum-stall
exit (kept — it protects in bear markets, per era-split evidence) complete the exit
engine. Dollar risk is constant: wider stop ⇒ smaller position.

### Sector funnel (Sunday)
11 macro sectors → 34 microsectors + 14 wildcards, scored vs SPY → **top 5** macro
(top-3 caught the eventual best sector only 36% of weeks; top-5 catches 51% — E9)
→ ~100 weekly deep-scan candidates. The ranking formula (50/30/20) stays until the
live formula race graduates a challenger (accel-only leads the 27-year test).

### Super-Performer discovery (E15)
Minervini-inspired: superperformance happens in mid/small caps *before* they join
the big indexes. Sundays: Trend Template screen over the S&P MidCap 400 + SmallCap
600 (names outside our universe) → top-15 RS shortlist → **one-time company
dossiers** (web research: product, catalyst, management, red flags — written once,
updated with dated deltas) → ADMIT/WATCH/PASS. Admits join the growth universe and
face every normal gate. No quotas: zero admits on a quiet Sunday is a good outcome.

## Automated schedule

Managed via Claude Code scheduled tasks (fire while the app is open; the daily scan
also has a guarded launchd runner in `scripts/`):

| Task | When | What |
|---|---|---|
| Universe refresh | Sun 7 AM | Live scrape S&P 500 + Nasdaq 100 → ~458 tickers |
| Growth universe refresh | Sun 7:30 AM | Validate curated small/mid-cap list |
| Weekly review | Sun 8 AM | Sentiment refresh (126 tickers) → sector funnel (11 macros) → discovery scan + dossiers → outcome review → formula race scoring → **EXPERIMENTS.md re-scoring** → Monday briefing |
| Daily scan | 9:35 AM Mon–Fri | Deep pipeline (~100 stocks) + swing funnel + auto-entries/exits + stop re-eval + chart refresh (3+/7 only, 3-day validity) |
| Decision capture | 4:30 PM Mon–Fri | "Did you invest?" for each BUY |
| Paper report | 5 PM Mon–Fri | Evening P&L |

## Running costs (measured, not estimated)

| | Cost |
|---|---|
| Market data (yfinance, all of it) | $0 |
| Daily scan LLM (deep-scan analysis + chart vision ~45–55 charts) | ≈ $1.50–2/day |
| Sunday (Haiku sentiment for 126 tickers; everything else is math or subscription-covered) | < $0.10/week |
| **Total** | **≈ $35–45/month** |

Rules learned in production (see EXPERIMENTS.md incidents): budget approvals are
one-time and purpose-specific; any paid-API loop must prove its save path on unit
one and abort on failure after a paid call.

## Dashboard

`PYTHONPATH=. .venv/bin/python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000`

| Tab | Shows |
|---|---|
| Signals | BUY/WATCHLIST/AVOID + swing candidates with chart verdicts |
| Swing / Long-Term | Positions, exit alerts, chart modals; LT has the 👁 Shadow Tracking panel |
| Portfolio | Unified positions + **Closed Trades** (realized P&L with exit reasons) |
| Decisions | Date-wise activity log |
| Weekly Review | Sector funnel + ⚡ accel radar + 📊 5-year sector backtest + feedback/mistake panels |
| **Super Performers ★** | Discovery shortlist: company names, RS percentile, 52w positioning, dossier status |

## Manual commands

```bash
# Daily scan / swing scan / weekly funnel
PYTHONPATH=. .venv/bin/python workflows/daily_scan.py
PYTHONPATH=. .venv/bin/python workflows/run_swing_scan.py
PYTHONPATH=. .venv/bin/python workflows/weekly_scan.py

# Discovery scan (mid/small caps)
PYTHONPATH=. .venv/bin/python core/discovery.py

# Backtests
PYTHONPATH=. .venv/bin/python -c "from core.sector_backtest import run_backtest; run_backtest()"
PYTHONPATH=. .venv/bin/python -c "from core.strategy_backtest import run_all; run_all()"
PYTHONPATH=. .venv/bin/python -c "from core.strategy_backtest import trend_template_backtest; trend_template_backtest()"

# Sentiment cache refresh (Sunday's job, manual)
PYTHONPATH=. .venv/bin/python workflows/bigdata_refresh.py [--status]
```

## Key data files

```
data/
  universe_cache.json             ~458 tickers, 7d TTL (stale fallback up to 21d)
  weekly_universe_{date}.json     Sunday funnel output (deep scan reads all week)
  ohlcv_cache_{date}.parquet      Daily 400d batch OHLCV — one download serves
                                  prefilter, charts, S/R, trend template
  trend_template_{date}.json      Daily Minervini template flags + RS percentiles
  swing_candidates_{date}.json    Swing scan output incl. chart verdicts
  discovery_{date}.json           Super-Performer shortlist (Sundays)
  dossiers/{ticker}.md            One-time company research, dated deltas appended
  paper_portfolio.json            LT paper portfolio     growth_portfolio.json  Swing
  paper_trades.jsonl / growth_trades.json   Full trade logs (Closed Trades tab)
  signal_outcomes.jsonl           Every signal + shadow trade, scored 30/90d
  screen_performance.json         Feedback records with strict:*/source:* tags
  swing_gate_state.json           E7 gate adaptation state + decision history
  sector_ranking_log.jsonl        E9 live formula race (all challengers, weekly)
  sector_backtest.json            E9 5y backtest + walk-forward (dashboard panel)
  mistake_log.json                Learned loss patterns
  bigdata_cache/{ticker}.json     Weekly sentiment (yfinance + Haiku — no paid APIs)
```

## Known gotchas

- **yfinance rate limits**: never per-ticker loops over the universe — one batch
  `yf.download`. The parquet cache exists for this.
- **Silent `except: pass`** has repeatedly hidden dead signals and skipped
  microsectors. A signal that never fires is a bug until proven otherwise.
- **Paid-API loops**: prove the save path with one unit before looping; abort on
  any failure after a paid call (see the E13 incident).
- Dashboard restart after backend changes: `pkill -f "uvicorn dashboard.app"`
  (launchd resurrects it) + hard-refresh the browser.
- Wikipedia scrape needs `verify=False` on this machine.

---

**Started:** June 2026 · **Author:** Soham Patil · **License:** MIT
For the full decision history, read `EXPERIMENTS.md` — it is the project's memory.
