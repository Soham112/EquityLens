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

### E16 — P/E-expansion topping exit (Minervini ch. 3) (SHIPPED)
- **Hypothesis:** A superperformance run tops when price outruns earnings — the P/E
  balloons to 2.5-3x its breakout level *while* quarterly growth decelerates. Detecting
  that specific combination should flag tops earlier (or catch ones the trail stop
  misses) without cutting healthy runs where the P/E expands but earnings keep pace
  (Apollo Group, CKE — P/E flat/expanding, growth intact, kept running).
- **Change (live 2026-07-11):** swing entries now record `breakout_pe` (trailing P/E at
  fill) on the `GrowthPosition`. `momentum_monitor.check_pe_expansion()` raises a
  `PE_EXPANSION_TOP` alert (NEXT_SESSION urgency) only when **both** hold:
  current P/E ≥ 2.5× breakout P/E **AND** `revenue_growth_trend == DECELERATING`.
  Expansion with growth intact is deliberately left to run. Alert, not auto-exit —
  it tells the operator to hunt for sell signals / price weakness.
- **Why an exit and not an entry:** the book's high-P/E case studies are survivorship-
  biased, so high P/E is unsafe as a *buy* signal. As a topping signal on names already
  held for other reasons there's no graveyard sampling — pure position management.
- **Known limitation:** deceleration is proxied by *revenue* trend. The planned EPS-
  velocity replacement was tested in E17 and FAILED (no cross-sectional edge), so the
  revenue-trend proxy stays — a direct EPS swap is not justified unless a future,
  separately pre-registered EPS formulation earns it. Only names entered after
  2026-07-11 carry a `breakout_pe` (no retroactive baseline);
  unprofitable names (no trailing P/E) never trigger — correct.
- **How to judge (pre-registered):** over the next ~20 swing exits, for every position
  that fires `PE_EXPANSION_TOP`, measure the forward 10- and 20-trading-day return from
  the alert. WORKED if flagged names draw down (or underperform the swing book) more
  than un-flagged concurrent holds, i.e. the alert leads weakness. FAILED if flagged
  names keep climbing (we'd be clipping winners — the Apollo/CKE false-positive case).
  Watch the fire rate: the deceleration gate should make this rare; if it never fires
  across a full cohort, loosen toward P/E-alone-with-price-weakness rather than growth.
- **Status: OBSERVING (live since 2026-07-11).** No fires yet — the two open growth
  positions predate the `breakout_pe` field.

### E17 — Earnings-growth velocity as a scored entry factor (BACKTESTED → FAILED, not shipped)
- **Hypothesis:** Minervini's actual superperformance driver is *earnings* growth
  ("Apollo earning 40% per annum… the P/E takes care of itself"), but
  `growth_hunter._score_revenue` scores **revenue** growth + acceleration and margins —
  EPS-growth velocity is scored nowhere on the trading track. Adding a quarterly-
  EPS-growth / earnings-acceleration factor should improve the E15 Super-Performer
  discovery set's hit rate on the profitable-compounder case.
- **Why backtest-first, NOT shipped:** this changes *what gets bought*, and E14 is the
  standing warning that a plausible Minervini rule (the Trend Template) FAILED its
  10-year backtest (beat baseline in only 4/11 years). No production wiring until the
  history says it helps.
- **Pre-registered test (write before running):** on the E15 discovery universe over a
  historical window, rank candidates with vs. without an EPS-growth-velocity factor
  added to `growth_hunter`. Success = the EPS-augmented ranking's top decile shows a
  higher forward hit rate / avg return than the current rubric's top decile, era-split
  stable (don't accept a single-era win — same discipline as E11/E14). No paid API in
  the loop (yfinance earnings history only) — respects the vision-backtest cost rule.
- **Backtest built & run 2026-07-11** (`core/eps_velocity_backtest.py`, yfinance-only;
  caches to `data/e17_{prices.parquet,eps.json}`, delete to re-fetch):
  GROWTH_UNIVERSE (61 names), 40 quarter-start rebalances 2016-2025. Point-in-time EPS
  from `get_earnings_dates()` (Reported EPS tagged with actual report date; only quarters
  reported *before* each rebalance used — no lookahead). Per date, ranked candidates with
  ≥5 clean quarters (positive year-ago base) three ways: **EQL** (universe avg, no factor),
  **YoY** (top quintile by EPS-growth level), **VELO** (top quintile by EPS *acceleration* —
  the factor on trial). 864 (date,ticker) records. Top-quintile used (universe too small
  for a decile) — a documented deviation from the pre-registered "top decile."
- **RESULT — FAILED.** fwd63 mean return, VELO vs the universe average it had to beat:
  overall 5.43% vs **5.90%**; 2016-2020 7.33% vs **7.56%**; 2021-2026 3.91% vs **4.66%** —
  VELO **loses to plain equal-weight in BOTH eras** and beat EQL in only **4/10 years**
  (and beat the simpler YoY factor in only 4/10). Its `beatEQL%` stayed 38-42% (a velocity
  pick beat the universe avg <half the time). VELO's *mean* looked higher at fwd126
  (14.8% vs 13.6%) but its *median* was far worse (0.4% vs 3.9%) with lower hit rate —
  outlier-driven, not a robust edge. Fails the pre-registered primary test outright.
- **Decision: do NOT wire EPS-velocity into `growth_hunter`.** Same lesson as E14 — a
  plausible Minervini-flavored rule that history rejects. Backtest-first paid off: zero
  production risk taken. E16's revenue-trend deceleration proxy therefore stays as-is
  (no EPS-velocity replacement earned its place).
- **Caveats (honest bounds, none rescue the result):** (1) universe is today's
  survivorship-biased list — inflates absolute returns but hits all three strategies
  equally, so the *relative* null stands; (2) factor only scores profitable names with a
  positive year-ago base (correct scoping to the compounder case), leaving ~864 records;
  (3) any different formulation (smoothed 3-qtr slope, velocity×level interaction) would
  be a NEW pre-registered experiment, not a reinterpretation of this one — no post-hoc
  fishing.
- **Status: FAILED (backtested 2026-07-11, not shipped).**

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
- **Date started:** 2026-07-09
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
- **Date:** 2026-07-09
- **Change:** none to production yet — `core/sector_backtest.py` replays the live
  ranking formula (50% rel-return / 30% accel / 20% breadth, exact normalizations)
  weekly over 2021-01 → 2026-07 (287 weeks, high fidelity — live funnel also scores
  off ETF price/volume). Dashboard: "📊 5-Year Sector Backtest" button on Weekly
  Review; API `/api/backtest/sectors` (`?refresh=true` recomputes).
- **Why backtest, not forward-observe:** the funnel is pure math — mechanically
  replayable. Doctrine (from user, 2026-07-09): backtest what is replayable;
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
- **Out-of-sample validation (2026-07-09, `walk_forward()`, top-5 baskets,
  pre-registered rule: beat live in ≥3/4 test years by ≥0.10%/4w):**
  - Survivors: 40/40/20 (3/4, +0.32%/4w), accel-only (3/4, +0.27%), equal thirds
    (**4/4**, +0.26%). Failed: return-only, 70/30/0, 60/20/20 (0-1/4).
  - Robust directional finding: every survivor shifts weight FROM relative return
    TOWARD momentum acceleration. Accel-only had the worst 2022 bear (−0.53% vs
    live −0.23%) — single-factor fragility; ruled out.
  - Walk-forward selection (+1.57%) barely beat always-live (+1.48%) — no magic
    in chasing the recent best formula.
- **DECISION (user, 2026-07-09):** top-5 cutoff → PRODUCTION (weekly_scan now runs
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
- **Long-history bias check (2026-07-09, user-requested: "was there any bias that
  led equal thirds to a win?"):** `long_history_validation()` — same replay over
  1999-2026 (8 sectors, XLC/XLRE too young; top-4 of 8 = same proportion;
  1,379 weeks, 27 years; pre-registered: majority of years + positive margin +
  not materially worse in crisis years 2000-02/2008/2020/2022).
  - **Equal thirds FAILED**: 13/27 years (48%), +0.06%/4w, crisis −0.04%. Its
    4/4 walk-forward win was a 2021-26 regime artifact — user's bias suspicion
    vindicated. NOT a production candidate.
  - **Accel-only PASSED — the only one**: 19/27 years (70%), +0.21%/4w, and the
    BEST crisis record (+0.17%): dot-com bust −0.40% vs live −0.92%, GFC +0.35%
    vs −0.26%. Corrects the earlier "fragile in bears" call, which was itself
    small-sample bias from 2022 alone; its real weakness is V-shaped whipsaws
    (2020, 2022).
  - 40/40/20 failed on crisis margin (−0.11%). Lesson for the log: the two
    validation windows crowned DIFFERENT winners — exactly why nothing ships
    without the live race.
- **Accel radar (built 2026-07-09, user idea):** weekly_scan now flags sectors in
  accel-only's top-3 that the production formula left out of its top-5
  (`accel_radar` in weekly universe + ⚡ callout on the funnel dashboard) —
  "about to turn" watch list, attention only, never entries. First preview:
  materials.
- **2026-07-09 coverage expansion:** consumer_staples added as the 11th macro
  (XLP — the funnel's defensive-rotation blind spot; XLP trades since 1998 so
  long-history validation includes it automatically) + 8 new micros (staples,
  insurance, crypto_financials, managed_care, travel_leisure, streaming_media,
  transports_airlines, midstream_natgas) on top of ai_photonics/power_grid.
  Formula-race note: ranking-log entries before this date have 10 sectors,
  later ones 11 — score_ranking_log handles both; top-5-of-11 vs top-5-of-10
  is a mild comparability shift, noted here so nobody mistakes it for drift.
- **Status: OBSERVING (Phase 2 live race running — now refereeing live 50/30/20
  vs accel-only as the surviving challenger; top-5 cutoff settled).**

### E10 — Accel-radar probe: flagged sectors get an audition, not a bypass
- **Date started:** 2026-07-09
- **Change:** each Sunday, for every radar sector (accel-only top-3 outside the
  production top-5), the strongest microsector's stocks join the weekly deep-scan
  universe as probe candidates (`radar_candidates` in the universe file, shown in
  the funnel's ⚡ callout). Same 7-agent pipeline, same conviction bar, same gates.
  Signal records tagged `source="radar"`; cohort visible as `radar_sourced` in
  `/api/feedback/shadow`.
- **Hypothesis:** radar sectors averaged +1.48%/4w vs +1.11% for production's own
  top-5 (2021-26, 431 sector-weeks) — the best-performing group was the one we
  never analyzed. Radar width stays top-3: accel ranks 4-5 decay to +1.27%.
- **How to judge (pre-registered):** after ≥15 scored radar-sourced signals,
  compare hit rate and avg 90d return vs funnel-sourced signals. Match-or-beat →
  keep (consider widening probe). Clearly worse → sector-level edge doesn't
  survive stock selection; drop the probe, keep the watch-only callout.
- **Status: OBSERVING** — first probe queued for Sunday 2026-07-12:
  materials → metals_mining → FCX, NEM, GOLD, AA, CLF, MP.

### E11 — Swing strategy layer backtests (screener + exit engine, 2022-2026)
- **Date:** 2026-07-09 — `core/strategy_backtest.py`, results in data/strategy_backtest.json.
  ~50k weekly prefilter-passing snapshots; 4,000 sampled candidate entries × 7 exit
  variants × 63d daily-close simulation. Honesty notes: only the 3 OHLCV-replayable
  signals (volume/RS/structure); today's constituents (mild survivorship); overlapping
  entries grade rule quality, not equity. LT deliberately NOT backtested (no
  point-in-time fundamentals — would be look-ahead theater; E8 shadow is the honest tool).
- **Part A findings (screener layer):** signal-count cohorts are essentially FLAT at
  21d fwd (0 signals +0.88%, 1 +0.77%, 2 +1.02%, 3 +0.94%; win rates all ~53%).
  The prefilter does the heavy lifting; the price signals add little ranking power.
  Per-signal: price_structure has the only real edge (+1.02% fired vs +0.80% quiet,
  n=18k); relative_strength ≈ nothing; **volume_accumulation slightly ANTI-predictive**
  (+0.74% fired vs +0.90% quiet) — watch its live screen stats for a KILL.
  Implication for E7: signal-count thresholds matter less than assumed → loosening
  4→3 unlikely to hurt via count alone; the alpha burden sits on chart vision +
  gates → raises the value of the sampled vision backtest (user approved ~$5 ≈
  350-400 charts; not yet run).
- **Part B findings (exit engine — the levers matter enormously; variant spread
  0.64%-1.56% avg/trade, win 37-53%):**
  - Live config: +1.07% avg, 49% win, 17.6d hold. Middle of the pack.
  - **Stall exit is the biggest lever**: it ends 63% of trades. Removing it → +1.56%
    avg (best) but 37% win, −4.95% median, 38d holds — stall trades expectancy for
    smoothness and capital turnover. Debatable, not obviously wrong.
  - **"S1 only" (drop the 2.5×ATR floor) beat live on every central metric**:
    +1.38% avg, +0.42% median, 53% win — wider structural stops stop-hunt less
    (743 stop-outs vs live's 1291). Worst trade −41% vs −36%, but live sizing
    caps $ risk via stop distance, so wider stop = smaller position anyway.
  - Tighter floor (2.0×ATR) worse; earlier trail worse (cuts winners); 21d time
    stop much worse (+0.64%) — swing winners need more than 21 days.
- **Era-split stability check (2026-07-09):**
  - **"S1 only" STABLE — passes.** Beats live on avg AND median AND win rate in
    BOTH eras: 2022-23 (+0.45%/+0.20%/52.5% vs live +0.31%/−0.24%/48.2%) and
    2024-26 (+1.89%/+0.53%/53.5% vs +1.50%/−0.02%/49.7%). Candidate production
    change (needs user decision): stop = S1−0.5×ATR primary, 2.5×ATR only as
    fallback when no tested S1 exists — i.e., remove the floor's override.
  - **"No stall exit" REGIME-DEPENDENT — fails.** Its overall win was all
    2024-26 bull (+2.36%); in 2022-23 it did +0.13% at 32% win — worse than live.
    The stall exit earns its keep in bear/chop. KEEP. (Equal-thirds trap caught
    again by the split.)
  - Vision-agent prep for the sampled backtest: pattern vocabulary expanded to 18
    (incl. inverse H&S, symmetrical triangle, triple top/bottom, wedges, pennant,
    rounding bottom, flat base) + anti-bias discipline rules (bearish patterns
    weigh equally; "none" is honest; conflict → wait). Production prompt and
    backtest prompt are now the same thing being tested.
- **Status: OBSERVING.** S1-only change awaiting user decision; vision sample
  (~$5, ~350-400 charts) approved and queued.

### E11b — insider_buying signal: live fetch → Sunday cache (cost fix, not removal)
- **Date:** 2026-07-09. Audit: fired 0 times in 522 candidates over 7 scan days
  while costing ~150 per-ticker yfinance calls/day. User questioned keeping it.
- **Decision:** KEEP in the 7-signal scale (removal would rescale every threshold
  and break E7 cohort comparability) but read from the weekly sentiment cache's
  `yfinance_insider` block — zero scan-time cost. Tickers outside the cache don't
  fire (same outcome as before: it never fired anyway). short_squeeze untouched
  (fires 5%, computed from already-fetched data). Per-screen hit rates will judge
  both as live data accrues.

### E12 — Swing stops: S1 primary, ATR floor demoted to fallback (SHIPPED)
- **Date shipped:** 2026-07-09 (user decision, on E11's era-split evidence)
- **Change:** swing chart stop = S1 − 0.5×ATR whenever a tested S1 exists;
  entry − 2.5×ATR only as fallback (was: max() of both — the floor override
  tightened stops into stop-hunt range). LT weekly stop formula UNCHANGED
  (E11 evidence was daily/swing bars only).
- **Evidence:** E11 era split — beats old formula on avg AND median AND win rate
  in both 2022-23 and 2024-26. Dollar risk unchanged (risk-cap sizing scales
  position to stop distance; wider stop = smaller position).
- **How to judge live:** stop-out rate and avg return of post-E12 swing exits vs
  the pre-E12 cohort (closed-trade log). Also expect fewer HARD_STOP exits.
- **Status: OBSERVING (live since 2026-07-09).**

### E13 — Sampled chart-vision backtest: grading the analyst (~$5, one-time)
- **Date started:** 2026-07-09. Budget note: the ~$5 is ONE-TIME for this
  backtest — never scale daily vision spend without explicit approval (user).
- **Change (prep, production):** vision prompt → 18-pattern vocabulary +
  anti-bias rules; charts now carry MA200 + 52w-high line; prefilter OHLCV cache
  400d (was 90d) so charts get long context from the same single batch download;
  numeric MA200/52w-high context added to the prompt.
- **Method:** ~350 historical screener candidates, stratified half 2022-23 /
  half 2024-26, chart rendered AS OF the date with the production renderer,
  analyzed with the production prompt, outcomes simulated with production (E12)
  exits. Incremental jsonl — interruption loses nothing paid for.
- **How to judge (pre-registered):** vision-ENTER (actionable entry_type +
  price in zone) must beat blind-entry-everyone on avg sim return AND win rate,
  and vision-WAIT must underperform ENTER. If ENTER ≈ everyone, vision is
  theater and the chart budget should be cut, not raised. Secondary: per-pattern
  and per-entry-type stats (needs ≥8 charts per pattern to report).
- **INCIDENT (2026-07-09 run): FAILED — ~$10 billed, ZERO records saved.** All
  ~350 vision calls succeeded (billed) but every save crashed on a numpy int64
  in json.dumps; the per-chart try/except swallowed the error and kept spending.
  Cost also 2x estimate ($0.030/chart real vs $0.014 assumed — guard capped
  chart count, not dollars). Harness fixed: native-type coercion + default=float,
  fail-fast proof-of-save after chart #1, and any failure after a paid call now
  ABORTS the run. Hard rules recorded in memory.
- **Decision (user, 2026-07-09): free path first.** `grade_live_verdicts()` scores
  the vision verdicts every daily scan already saves (no API cost, windows mature
  daily; Sunday task re-runs it). A ~$10 bear-era-weighted study (300-400 charts,
  all 2022-23) is pre-approved in principle for LATER, on user's go signal only.
- **First free grading (2026-07-09, 66 verdicts, windows 0-8 trading days — far
  too young for conclusions):** enter_in_zone −0.33% avg / 52% win (n=29 latest)
  vs wait +0.84% / 48% (n=25) — the WAIT cohort is ahead so far, i.e. mildly
  inverted. NOT actionable at this sample size; logged so nobody can say later we
  didn't see it early. Alarm threshold (pre-registered): inversion persisting at
  n≥50 with fwd10+ windows.
- **Status: OBSERVING (free grading weekly; paid bear-era study deferred).**

### E14 — Minervini Trend Template backtest (2016-2026, 10y per user decision)
- **Date:** 2026-07-10. `trend_template_backtest()` — full 8-point template
  (price > MA50 > MA150 > MA200, MA200 rising 1mo, ≥30% above 52w low, within
  25% of 52w high, RS percentile ≥70 cross-sectional) replayed weekly,
  234k stock-week observations, per-year cohorts. Survivorship noted (today's
  constituents); cohort comparisons partially cancel it.
- **Pre-registered rule:** template cohort must beat BOTH baselines (current
  price_structure check AND all prefilter passers) on fwd21 in a majority of
  years incl. one down year. **RESULT: FAILED** — beat both in only 4/11 years.
- **But the split verdict matters:**
  - vs our CURRENT structure check: template won **11/11 years** (incl. both
    down years: 2018 +0.01% vs −0.61%; 2022 −0.87% vs −1.70%). It is a strictly
    better version of what price_structure tries to be.
  - vs simply taking every prefilter passer: no 21-day edge (+1.50% vs +1.54%
    avg) — at the month scale, selectivity itself isn't alpha (consistent with
    E11's flat signal-count finding). Caveat: fwd21 grades a marathon runner on
    a sprint — the template targets months-long runs; a fwd63 check is the
    fair test before any production signal change.
- **Decision:** adopt the template as the LAYER-1 DISCOVERY SCREEN for the
  small/mid-cap expansion (its true Minervini purpose: narrowing ~1,000 names
  to a research shortlist — there it must only beat our structure check, which
  it does 11/11). NO change to the live price_structure signal yet (queue:
  re-test at fwd63 before considering replacement).

### E15 — Trend Template live + Super-Performer discovery (Phase 1)
- **Date shipped:** 2026-07-10 (user decision after E14's two-horizon confirmation:
  template beat the old structure check **11/11 years at BOTH fwd21 and fwd63** —
  fwd63 +4.33% vs +3.05%).
- **Changes:**
  1. `price_structure` signal = Minervini Trend Template (8 criteria incl.
     MA50>150>200 stack, ≥30% above 52w low, cross-sectional RS pct ≥70).
     Computed in the prefilter batch (needs everyone's returns for the RS rank),
     written to data/trend_template_{date}.json, read by the signal; legacy
     Stage-2 check is the fallback when no file; flat-base branch kept.
  2. `core/discovery.py` — weekly (Sunday) Trend Template screen over the
     S&P MidCap 400 + SmallCap 600 (~1,000 names outside our universe), $5M/day
     liquidity floor, top-15 by RS pct → data/discovery_{date}.json + the new
     "Super Performers" dashboard tab (/api/discovery). NAMES ONLY — research
     candidates for Phase 2 dossiers; approved names enter via growth_universe
     and face the same gates. No parallel pipeline.
- **First live results:** 458-universe: MGM passes (RS 88), NVDA fails (RS 68).
  Discovery: 177/1000 mid/smalls pass; shortlist RS 98-100 names up 87-611%
  off 52w lows (PENG, SEZL, ACMR, DAVE, LQDA, ...).
- **How to judge:** (a) live signal — price_structure's per-screen hit rate
  pre/post 2026-07-10 cohorts; (b) discovery — track shortlist names' forward
  performance and whether any graduate to growth_universe entries that win.
  Phase 3 (Haiku catalyst classifier) queued.
- **Phase 2 SHIPPED (2026-07-12) — composed, not parallel:** `core/dossier.py`
  writes a $0 data-skeleton dossier (metrics, fundamentals, technicals, risk
  flags) for every shortlist name during `discovery_scan`, leaving research +
  Verdict marked `PENDING RESEARCH`. Weekly-review STEP 2.5 was rewritten to
  FILL those sections in-place (gate = `needs_research()`/PENDING marker, was
  "missing file") and keep the ADMIT→growth_universe wiring. Dashboard: the
  Super-Performers "Dossier" 📁 is now a clickable viewer
  (`/api/discovery/dossier/{ticker}`). No paid API; touches no gate/threshold/score.
- **First full research pass (2026-07-12, 15 names):** 7 ADMIT → growth_universe
  (PENG, SEZL, DAVE, ACMR, LQDA, EXTR, VCYT — genuine accelerating growth), 5 WATCH
  (FTRE/PGNY turnaround-or-modest, ICHR/NEO cyclical/unprofitable, TWLO oversized),
  3 PASS. The 3 PASSes are the research layer earning its keep — all passed the
  technical screen but aren't superperformers: **OGN** (Sun Pharma $4B buyout) and
  **AVNS** (AIP $1.27B take-private) are M&A premiums, **AMN** (+100% rev) is
  one-time strike/labor-disruption revenue management guides to normalize down.
- **Status: OBSERVING** (track whether the 7 admits win vs the WATCH/PASS set).

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
