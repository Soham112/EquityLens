# tests

Stdlib `unittest` only — **no pytest, no installs**, so nothing lands in the
project `.venv`.

```bash
# from the repo root
.venv/bin/python -m unittest discover -s tests -v
```

## What is (and isn't) covered

These cover the **pure numeric helpers** that the trading logic and every
backtest conclusion rest on: horizon maths, S/R detection, base counting,
trade accounting, freshness maths. They are deterministic, need no network, no
API keys, and run in under a second.

They deliberately do **not** touch:
- yfinance / any network call (the one test that needs price history injects a
  fake `yf.Ticker`)
- files under `data/`, which change every scan — a test that reads live scan
  output would pass today and fail tomorrow for no good reason

## Why these particular tests

Most were written ad-hoc while fixing real bugs, then thrown away. Their value
is **regression protection**, not bug discovery: several of the bugs they cover
were wrong-by-design, so a test written at the time would have encoded the wrong
behaviour. What they buy is that a fix stays fixed.

That matters more than usual here because EXPERIMENTS.md conclusions (E11, E14,
E21, E23, E24) all depend on a small set of shared helpers. A silent change to
`_simulate`, `_s1_support` or `_calendar_to_trading_days` would quietly falsify
settled experiments with nothing to flag it.

Each test names the bug or experiment it guards.
