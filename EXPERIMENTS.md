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
