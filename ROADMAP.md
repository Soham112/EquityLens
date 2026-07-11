# EquityLens — Platform Roadmap & Design Decisions

> This file is the original design vision (June 2026). For what actually got
> built, what worked, and what the evidence changed, read **EXPERIMENTS.md** —
> the living decision log — and README.md for the current architecture.

## Vision
A self-improving AI equity research platform with two parallel trading tracks:
- **Long-term holdings** — fundamentals-driven, DCA-funded, conviction-based
- **Swing trades** — technical-pattern-driven, 2–6 week holds, profits fund long-term buys

The orchestrator uses LLMs only when numerical data is insufficient. Most decisions (screening, filtering, staleness checks) are pure math — fast, free, consistent.

---

## Capital Strategy
```
Swing trades     (30% of capital) → target +15–50% in 2–6 weeks
Long-term holds  (60% of capital) → conviction buys, DCA monthly
Cash/dry powder  (10% of capital) → always reserve for opportunities
```

Swing profits flow into the long-term bucket. Long-term positions are never sold on a signal — only added to via DCA.

### Position Promotion
A swing trade can be promoted when it hits its target but the thesis remains intact:
```
SWING → MOMENTUM → LONG-TERM
```
- **Swing**: Technical pattern entry, small size, hard exit at 15–20% OR stop
- **Momentum**: Pattern + Hunter 7+, trail stop 10–15% below peak, let winners run
- **Long-term**: Hunter 8+, fundamentals intact, DCA monthly, never sell on signal

Exit rules by track:
- **Low conviction** (pattern only): hard exit at 15–20%
- **High conviction** (pattern + Hunter 8+): trail stop, target can be 50%+
- **Promotion trigger**: price at target + vision re-scan still shows ADVANCING structure

---

## Pipeline Architecture

```
Universe (600 stocks)
    │
    ▼ core/screener.py  [NOT BUILT YET]
Named screens (pure yfinance math, free):
  golden_cross, breakout, multibagger,
  swing_setup, darvas, oversold_quality
    │
    ├── Swing candidates → agents/swing_screener.py [NOT BUILT YET]
    │     Haiku vision (cheap first-pass)
    │     → SWING signal if pattern found + confidence > 0.6
    │
    └── Long-term candidates → core/orchestrator.py [BUILT]
          Hunter → (Vision if score 6+) → Critic → Sentiment
          → Validator → BUY / WATCHLIST / AVOID
```

---

## Chart Vision System

### What it does
Sends a rendered candlestick chart to Claude's vision API and extracts:
- Dominant chart pattern (1 of 19 constrained patterns)
- MA crossover recency and quality (not just boolean MA50 > MA200)
- Price structure stage: BASING / ADVANCING / TOPPING / DISTRIBUTION / RECOVERY
- Support and resistance levels
- `chart_score_delta`: ±0 to ±1.0 adjustment to Hunter's technical score

### Two-timeframe design
| Chart | Period | Interval | Scan schedule |
|---|---|---|---|
| Weekly | 5y | 1wk | Sunday + when triggered |
| Daily | 1y | 1d | Trading days, only on 6+ scorers |

Weekly chart → "Is this stock in the right stage?"
Daily chart → "Is now the right time to act?"

### Vision cache (core/vision_cache.py) — BUILT
Chart patterns don't change daily. Cache is valid until a meaningful event occurs.

**Invalidation triggers (checked numerically, no API cost):**
1. Price breaks above lowest cached resistance → possible breakout
2. Price breaks below highest cached support → possible breakdown
3. Volume spike > 2× 20-day average → unusual activity
4. New MA crossover detected in last 5 sessions
5. Cache age > 7 days (weekly) or 3 days (daily) — force refresh

**Cost impact:** ~70–80% reduction in vision API calls.

### Cost breakdown (Sonnet 4.6)
- Per chart: ~$0.011 (~1 cent)
- Daily scan (20 stocks, cache hits expected): ~$0.05–$0.22/day
- Monthly: ~$5–7 total (core + swing combined)

---

## What's Built (v8.0 + Chart Vision)

| Component | File | Status |
|---|---|---|
| Price / fundamentals fetch | `core/data_layer.py` | ✓ |
| BigData.com client | `core/bigdata_client.py` | ✓ |
| Data staleness checker | `core/staleness.py` | ✓ |
| Market regime detector | `core/regime_detector.py` | ✓ |
| Conviction formula | `core/conviction.py` | ✓ |
| ATR stop system | `core/stop_loss.py` | ✓ |
| Main orchestrator | `core/orchestrator.py` | ✓ |
| Persistence (JSON + Supabase) | `core/persistence.py` | ✓ |
| Conviction monitor | `core/conviction_monitor.py` | ✓ |
| Earnings calendar gate | `core/earnings_calendar.py` | ✓ |
| Backtester | `core/backtest.py` | ✓ |
| Correlation clusters | `core/correlation.py` | ✓ |
| Universe (S&P 500 + Nasdaq 100) | `core/universe.py` | ✓ |
| Behavioral bias checker | `core/bias_check.py` | ✓ |
| **Chart renderer** | `core/chart_renderer.py` | ✓ NEW |
| **Vision cache** | `core/vision_cache.py` | ✓ NEW |
| Hunter agent | `agents/hunter.py` | ✓ |
| Critic agent | `agents/critic.py` | ✓ |
| Sentiment agent | `agents/sentiment.py` | ✓ |
| Validator agent | `agents/validator.py` | ✓ |
| Portfolio manager | `agents/portfolio_manager.py` | ✓ |
| Scout agent | `agents/scout.py` | ✓ |
| Journal agent | `agents/journal.py` | ✓ |
| **Chart vision agent** | `agents/chart_vision.py` | ✓ NEW |
| FastAPI dashboard | `dashboard/app.py` | ✓ |

---

## What's Next (Build Queue)

### A — Vision schedule fix
Wire `vision_cache.get_or_fetch()` into orchestrator for both timeframes.
- Weekly chart: called Sunday + on trigger
- Daily chart: called only when Hunter score >= 6 + on trigger
- Score gate: weekly vision at 6+, daily vision at 8+

### B — core/screener.py (named screens)
Pre-filter layer replacing static ticker lists. Pure yfinance math.
Named screens:
- `golden_cross` — MA50 crossed above MA200 within last 20 sessions
- `breakout` — within 5% of 52w high, volume > 1.5× avg
- `multibagger` — ROE > 15%, ROCE > 15%, P/B < 3, earnings growth > 10%
- `swing_setup` — RSI 45–60, near MA50, volume building
- `darvas` — new 52w high on volume, holding above prior box
- `oversold_quality` — RSI < 35, fundamentals still intact

### C — Swing pipeline
- `agents/swing_screener.py` — technical-first, Haiku vision first-pass
- `SWING` signal type (separate from BUY/WATCHLIST/AVOID)
- Position promotion logic: SWING → MOMENTUM → LONG-TERM
- Exit rules:
  - Low conviction: hard exit at 15–20%
  - High conviction: trail stop 10–15% below peak
  - At promotion: vision re-scan to confirm ADVANCING structure
- Swing position sizing: smaller (2–3%), tighter stops

### D — Dashboard restructure
New tabs alongside existing Signals / Paper Portfolio / Decisions / Weekly Review / Growth Scout:
- **Swing Trades** — entry, pattern, target, stop, days held, % to target, promote button
- **Long-Term** — thesis, Hunter score at entry, fundamental health today, next DCA date
- **DCA Positions** — avg cost basis, total invested, current value, next add date
- **Capital Overview** — 30% swing / 60% long / 10% cash allocation bar

### E — DCA tracker
- Scheduled monthly adds to conviction stocks
- Avg cost basis auto-calculated across all adds
- Separate from active signals — DCA positions never sold on signal
- Next add date shown on dashboard

### F — Feedback loop & mistake logger
- Per-screen hit rate tracking (did golden_cross actually work?)
- Signal → outcome wiring in journal
- Mistake pattern detector: "last 3 times RSI > 70 at entry, trade failed within 2 weeks"
- Paper trading mode: log everything, no real money

---

## Screen Philosophy — How We Know What Works
No screen is universally profitable. A golden cross works in trending markets,
fails in choppy sideways markets. The only honest answer is:

1. Run the screen → log the signal + entry price
2. Track what happened at +2w, +4w, +8w
3. After 20–30 trades per screen → calculate hit rate and avg return
4. Kill screens below 50% hit rate, double down on outperformers

The feedback loop (F above) builds this automatically over time.

---

## Paper Trading Timeline
- Month 1–3: Paper trade swings only. Get signal → outcome loop tight.
- Month 3–4: If swing hit rate > 55%, go live with small real money on swings.
- Month 4–6: Swing profits + confidence → fund first real long-term positions.
- Ongoing: DCA into conviction holds, let the system run.

Validation does not require 3 years. Validate the ENTRY SIGNAL in the first
60–90 days — did the stock behave as the pattern predicted?

---

## API Cost Summary
| Component | Cost |
|---|---|
| yfinance data | Free |
| Named screens (screener.py) | Free |
| BigData.com cache refresh | MCP (no token cost) |
| Chart vision (cached, smart triggers) | ~$5–7/month |
| Validator / thesis LLM calls | ~$3–5/month |
| Weekly review / briefings | ~$1–2/month |
| **Total estimated** | **~$10–15/month** |

A single 15% swing trade on $2,000 covers the full year of API costs.
