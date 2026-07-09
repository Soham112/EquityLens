# EquityLens — Experiment & Decision Log

A living record of every deliberate change to the system's behavior: the hypothesis
behind it, how we judge it, what the evidence said, and the verdict. Newest first
within each section. **Update rhythm:** the Sunday weekly-review task re-scores every
OBSERVING entry with the week's data; anyone changing trading logic adds an entry here
in the same commit.

**Verdicts:** `OBSERVING` (collecting data) · `WORKED` (kept, evidence attached) ·
`FAILED` (reverted/tightened, evidence attached) · `SELF-TIGHTENED` (system reverted it
autonomously via gate adaptation) · `INCONCLUSIVE`

How to judge = the pre-registered success measure. Write it BEFORE the data comes in —
that's what keeps this log honest.

---

## Active experiments

### E7 — Swing exploration mode: loosened, self-adapting entry gates
- **Date started:** 2026-07-08
- **Change:** selection gates loosened (signals 4+→3+, R/R 2.0→1.2, zone 2%→5%);
  entries failing a strict gate tagged `strict:<gate>` + sized 0.5x (probation);
  each gate self-tightens at 10+ closed cohort trades with hit rate <45% or avg
  return 3pts below clean cohort. Risk controls unchanged.
- **Hypothesis:** the strict gates were opinions, not evidence. More (smaller) entries
  produce the data to learn which gates actually protect returns. Suspected specifically:
  R/R-vs-nearest-resistance mismeasures near-high setups (see E-next-1).
- **How to judge:** per-gate cohort scoreboard at `/api/feedback/gates` — each cohort vs
  the clean cohort. System decides per gate at 10+ closed trades; log the outcome here.
- **Status: OBSERVING** — gate state: all loose. Zero cohort trades yet.
  - 2026-07-09 — first live run exposed a coupling bug: entry gate loosened to 3+/7
    but chart vision still only ran on 4+/7, so loose-eligible candidates reached
    auto-entry chartless and were silently skipped (MGM: 3/7, bounce, R/R 3.85).
    Fixed same day — chart threshold now follows the adaptive signals gate.
    First probation entry after fix: **MGM 3/7 bounce R/R 3.9, $121 (0.5x),
    tag strict:signals, stop $45.54**. Cost note: 3+/7 charting ≈ +25-30 vision
    calls/day vs 4+/7. Cohorts: signals=1 open, risk_reward=0, entry_zone=0.
  - _Weekly updates go here (date | cohort sizes | hit rates | any self-tightening)_

### E6 — Mistake patterns feed conviction scoring
- **Date started:** 2026-07-06
- **Change:** `mistake_conviction_penalty()` — learned loss patterns penalize matching
  new candidates (evidence-gated ≥3 occurrences, ALERT −1.0 / WARN −0.5, cap −1.5;
  swing side downsizes −15%/point instead).
- **Hypothesis:** "last time I bought something like this I got burned" — the disciplined
  version. Bounded so 3 bad trades can't codify recency bias.
- **How to judge:** when a pattern activates, compare trades it penalized vs. similar
  unpenalized history. Needs an active pattern first (mistake log currently empty —
  the initial "low hunter" pattern was a data artifact and was honestly removed).
- **Status: OBSERVING** (armed, silent — no active patterns yet)

### E5 — Feedback loop wired end-to-end (entries + exits scored)
- **Date started:** 2026-07-06
- **Change:** `record_exit()` on every automated exit, real fill prices at entry, swing
  entries create records with fired signals; closed trades visible on dashboard.
- **Hypothesis:** the system can't learn without closed-loop data; this is the substrate
  for E6/E7 and all future adaptation.
- **How to judge:** structural — % of exits that produce a scored record (target: 100%).
- **Status: WORKED** — first live run 2026-07-07/08 scored every exit
  (ONTO −17.5% LOSS, ISRG SCRATCH, RGTI SCRATCH). Pre-wiring positions backfilled.

---

### E8 — LT shadow tracking: measure the road not taken
- **Date started:** 2026-07-10
- **Change:** every gate that demotes a BUY → WATCHLIST now stamps its name on the
  signal (`demoted_by`: valuation_cap, macro_penalty, sector_gate, correlation_gate,
  earnings_gate, vix_pause, conviction_trend, mistake_pattern). Demoted signals and
  near-miss WATCHLISTs (conviction ≥7) are recorded in signal_outcomes.jsonl and
  scored at 30/90d like real signals — shadow trades, zero capital at risk.
  Scoreboard: `/api/feedback/shadow` + "Shadow Tracking" card on Weekly Review tab.
- **Hypothesis:** LT gates are untested opinions, but unlike swing (E7) we don't need
  live entries to test them — LT outcomes ≈ buy-and-hold, which a shadow measures.
  Prime suspect: the Graham-formula valuation cap structurally punishes growth names
  (AMD: conviction 9+, MOS −119%).
- **How to judge (pre-registered):** per-gate cohort avg 90d return vs entered-BUY
  cohort, minimum 15 scored signals per cohort. Blocked cohort beats entries by >2pts
  → gate is costing money, recalibrate it (as its own experiment). Lags by >2pts →
  gate earns its keep. Evidence-surfacing only — no auto-loosening of LT gates.
- **Status: OBSERVING** — 16 shadow records backfilled from 2026-07-08/09 scans
  (all valuation_cap: AMD, MRVL, TXN, BMY, MRK, EW, VRTX, VRT, ARM — entry prices
  from signal-date closes). First 90d verdicts expected ~October 2026; 30d interim
  reads ~August.
  - _Weekly updates go here (cohort sizes | avg 30/90d vs baseline | verdicts)_

### E9 — Sector funnel backtest (Phase 1: validate on history before production)
- **Date:** 2026-07-10
- **Change:** none to production yet — `core/sector_backtest.py` replays the live
  ranking formula (50% rel-return / 30% accel / 20% breadth, exact normalizations)
  weekly over 2021-01 → 2026-07 (287 weeks, high fidelity — live funnel also scores
  off ETF price/volume). Dashboard: "📊 5-Year Sector Backtest" button on Weekly
  Review; API `/api/backtest/sectors` (`?refresh=true` recomputes).
- **Why backtest, not forward-observe:** the funnel is pure math — mechanically
  replayable. Doctrine (from user, 2026-07-10): backtest what is replayable;
  forward-observe only what depends on live judgment (E7's chart vision).
- **FINDINGS (282 scored weeks):**
  - Top-3 avg forward-4w return +1.17% vs +1.11% for excluded sectors — **no
    meaningful edge**; the funnel buys focus/cost-control, not alpha.
  - Eventual best sector was in the top-3 only **36%** of weeks; a rank-4-6 sector
    beat ALL three picks **47%** of weeks (the blind-spot rate).
  - Cutoff: top-3 catches the best sector 36%, top-4 45%, top-5 51%.
  - Weight grid: 'accel only' +1.56%/4w and 'equal thirds' +1.45%/4w both beat the
    live mix (+1.15%) — hypothesis for recalibration, NOT a conclusion (single
    window, in-sample fit).
  - Rotation sim since 2021: formula top-3 1.98x vs SPY 2.19x vs equal-weight 2.06x.
- **Out-of-sample validation (2026-07-10, `walk_forward()`, top-5 baskets,
  pre-registered rule: beat live in ≥3/4 test years by ≥0.10%/4w):**
  - Survivors: 40/40/20 (3/4, +0.32%/4w), accel-only (3/4, +0.27%), equal thirds
    (**4/4**, +0.26%). Failed: return-only, 70/30/0, 60/20/20 (0-1/4).
  - Robust directional finding: every survivor shifts weight FROM relative return
    TOWARD momentum acceleration. Accel-only had the worst 2022 bear (−0.53% vs
    live −0.23%) — single-factor fragility; ruled out.
  - Walk-forward selection (+1.57%) barely beat always-live (+1.48%) — no magic
    in chasing the recent best formula.
- **DECISION (user, 2026-07-10):** top-5 cutoff → PRODUCTION (weekly_scan now runs
  `top_n_macro=5`; deep-scan universe ~60→~100 stocks). Weight formula stays
  50/30/20 — a challenger graduates only after the LIVE formula race confirms the
  backtest ("backtest first, actual results, then change production").
- **Phase 2 BUILT (the live race):** every Sunday `log_weekly_ranking()` logs the
  full 10-sector ranking under live weights AND every challenger
  (data/sector_ranking_log.jsonl); `score_ranking_log()` fills 4-week forward
  returns and reports avg top-5 basket return per formula. First entry 2026-07-09
  (live and equal-thirds already disagree on the top-5). Review the race in the
  Sunday task; revisit the weight decision when ~12+ live weeks are scored
  (~October 2026) — same evidence bar as the shadow cohorts.
- **Status: OBSERVING (Phase 2 live race running; top-5 settled → WORKED pending
  first weeks of wider-universe scans).**

## Settled experiments

### E4 — Valuation cap must demote the signal, not just the number (BUG FIX)
- **Date:** 2026-07-07 (found) → fixed same day
- **What happened:** valuation gate capped conviction to 7.0 on OVERVALUED names but
  left signal=BUY → AMD (MOS −119%) auto-bought. Conviction-trend penalty had the
  same flaw. Both now re-check the BUY threshold after adjusting, like the macro gate.
- **Evidence it mattered:** next scan (2026-07-08) demoted SEVEN would-have-been BUYs
  (AMD, MRVL, TXN, BMY, MRK, EW, VRTX — MOS −13% to −119%). Not a one-off.
- **Verdict: WORKED.** AMD position exited manually at +0.71% (no harm done).
- **Lesson:** any code path that adjusts conviction must re-evaluate the signal.

### E3 — Growth pipeline merged into main swing pipeline (no parallel pipelines)
- **Date:** 2026-07-05
- **What happened:** `growth_scan.py` was a second, ungated entry path (own schedule,
  direct `execute_buy`, no chart check) — source of all 4 June swing positions
  (SITM/RGTI/ONTO/BEAM, all entered on fundamentals alone).
- **Evidence:** those 4 ungated entries → 3 exited at losses (MNTS −26%, SITM −17%,
  RGTI −21%) before the merge; post-merge, SITM's re-entry attempt was correctly
  rejected by chart gate (entry=wait, R/R 0.61).
- **Verdict: WORKED.** Standing rule: extend pipelines in place, never bolt on a duplicate.

### E2 — BigData MCP replaced by yfinance + Haiku sentiment
- **Date:** 2026-06 (migration) → docs/task prompt fixed 2026-07-07
- **What happened:** paid subscription replaced by free yfinance data scored by Haiku
  (~$0.03/week), same cache format, agents unchanged.
- **Evidence:** 78/78 tickers on the free source; sentiment scores flowing (bounded ±1.5
  so precision loss is capped by design).
- **Verdict: WORKED.** Known gap: Yahoo headlines may miss litigation/SEC events →
  kill-switch coverage slightly weaker. Upgrade path if ever needed: SEC EDGAR 8-K search.

### E1 — Stop formula: S1 − 0.5×ATR with 2.5×ATR floor + weekly re-raise
- **Date:** 2026-07 (design), predates this log
- **Hypothesis:** support-based stops with a buffer survive stop-hunts better than pure
  ATR multiples; floor caps risk on near-high setups.
- **How to judge:** stopped-out trades that recovered >10% within 30d (stop too tight)
  vs. losses deeper than planned risk (stop too loose). Needs ~15 stop exits.
- **Status: OBSERVING** (few stop exits so far: MNTS hard stop worked as designed)

---

## Open questions / next experiments (not started)

- **N1 — Measured-move targets for near-high setups.** Nearest-resistance targets
  mechanically produce R/R 0.5–1.2 for stocks near 52w highs (2026-07-08: 1 of ~12
  charted setups cleared R/R 2.0). E7's `strict:risk_reward` cohort will show whether
  low-measured-R/R entries actually lose; if they WIN, the fix is the target formula,
  not the gate. Decide after that cohort reaches 10 closed.
- **N2 — Re-entry cooldown after thesis-break exits.** RGTI cycled enter→exit→re-enter→
  exit in 3 days (net ≈ flat, all rule-compliant). If churn like this recurs and loses
  money, add N-day cooldown after THESIS_BREAK exits. Watching.
- **N3 — Scheduler reliability.** Daily scan missed/ran late 3 sessions running (only
  fires while the app is open). Options: keep manual catch-up, or move to launchd like
  the dashboard. Decide if misses keep happening.
- **N4 — Insider signal never fires in mega-caps** (0/85 candidates 2026-07-08; verified
  alive, executives just don't open-market-buy). Consider replacing with a signal that
  has variance in this universe, or accept it as a rare-but-strong tiebreaker.
- **N5 — LT track cohort tagging.** E7 covers swing only. If exploration proves out,
  apply the same tag-and-judge pattern to LT gates (valuation cap threshold, macro
  penalty size).
