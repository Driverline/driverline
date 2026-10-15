# TradeLens AI signal engine: research infrastructure (Phases 2 and 3)

Status: research only. Nothing here publishes a signal or changes what members see.

## What exists
- `app/engine/store.py`: tables `market_candles`, `research_datasets`, `model_versions`, `research_runs`, `research_trials`
  (new, additive, RLS on automatically). Model status is constrained by the database to `research_only`,
  `demo_validated`, `approved`, `suspended`.
- `app/engine/validate.py`: dataset checks (order, duplicates, grid alignment, gaps, price sanity, extreme bars) and a
  checksum. An `invalid` dataset cannot be used for research. Candles that change after validation are refused.
- `app/engine/features.py`: the single feature definition, used by research and by live inference. Fixed 300-bar trailing
  window, no look-ahead, no recursive averages. `feature_spec_hash()` is recorded with every model version.
- `app/engine/research.py`: outcome definition, chronological 60/20/20 split, baselines, block-bootstrap intervals, drawdown,
  regime breakdown and calibration. The test segment (last 20%) is never evaluated here; `store.unlock_test(run_id)` allows
  it once per run.
- `app/engine/jobs.py`: admin-only jobs (probe, ingest, validate, baselines) and `/api/admin/engine*` routes.
- Admin page card "AI signal engine research (Step and Range Break)".

## Outcome definition (fixed before testing)
Signal at a bar's close, entry at the next bar's open, stop = 1.5 x ATR14, target = 1.0 / 1.5 / 2.0 x the stop distance,
first touch decides (stop first if both in one bar), horizon 24 M5 bars, cost 0.05 ATR, trades never overlap, and a trade
must finish inside its own segment.

## Release criteria (agreed in Phase 1)
- Research Only: default for everything.
- Demo Validated: at least 300 trades in the untouched test, the 95% interval for net expectancy above zero after costs,
  better than the baselines, positive in at least two of three sub-periods.
- Approved: Demo Validated plus at least 100 forward-demo signals over four or more weeks with no suspension trigger.

## Limitations
- Symbol codes for Step 200 to 500 and Range Break are unconfirmed until the probe has run against Deriv.
- Deriv's candle history depth for these symbols is unknown until the probe has run.
- Raw ticks are not stored. Tick statistics (up/down counts, longest runs, direction changes) are stored per M5 bar for the
  days that were ingested with tick statistics.
- The baselines measure drift and costs. They are not strategies.

## Safeguards shipped with it
- Daily analysis quota is reserved atomically (`auth.reserve_quota`), so parallel requests cannot pass the limit; a failed
  analysis gives its credit back.
- Opening a signal takes a per-member advisory lock on Postgres so parallel opens cannot pass the daily limit or the balance.
- Step Index: no new setups from the legacy scanner (`paused_instruments`); open ones are still tracked.
  Resume with `DRIVERLINE_FEED_CONFIG` containing `"paused_instruments": []`.

## Rollback
- Revert the commit. The new tables are additive and unused by the rest of the app, so they can stay or be dropped
  (`research_trials`, `research_runs`, `model_versions`, `research_datasets`, `market_candles`).
- To resume Step Index in the legacy feed only, set `paused_instruments` to `[]`.

## Tests
`python tests/test_engine.py` (or `python -m pytest tests`). No network needed: Deriv is replaced by a deterministic fake.


## Phase 3: pre-registered structure tests (`app/engine/structure.py`)
Question answered first: is there ANY structure to exploit in a symbol, before any strategy is built on it?
- Hypotheses are fixed in code and registered as immutable model versions (`hypothesis:<family>:<name>`) before each run.
- Step Index tests: direction runs, return autocorrelation (lags 1 to 10), variance ratios (2 to 32 bars), volatility
  clustering, hour of the day, baseline results against order-shuffled surrogates, and bar-level reversal.
- Range Break adds event studies with the direction fixed in advance: breakout continuation, failed breakout reversal and
  boundary-touch reversal against the prior 48-bar range.
- Every test runs on the development and the validation segment separately. p-values are Holm-adjusted within each
  segment. A pattern is REPLICATED only if it is significant after adjustment in both segments, in the same direction.
- Surrogate test: bar shapes (open to close, high, low) are shuffled to destroy order, then the baselines are rerun; an
  apparent edge that comes from how stops and targets are defined shows up in the surrogates too. The surrogates are built
  from development and validation bars only.
- The untouched test segment is never read (a test tampers with it and checks that nothing changes).
- Calibration on planted data: planted memory is found; fat-tailed data with no memory and fair random walks are not.
- Admin: "Run structure tests" per dataset. Results are condensed on the Admin card; every individual test is a row in
  `research_trials`.
- Probe now reports how many hours of ticks Deriv serves; the tick download reports how far back it got and why it stopped.

A replicated pattern is a candidate only. It stays Research Only until it passes the untouched test and forward demo.


## Phase 3b: diagnostics (`app/engine/diagnostics.py`)
A structure test says a pattern exists. The diagnostics ask whether a member could use it.
- Outcome breakdown: shares of targets, stops and timeouts and the average result of each (a hit rate below break-even with a
  positive average means the profit is not coming from the target).
- Entry delay: the same trades entered 0, 1, 2, 3 and 6 bars late (5-minute bars). Costs 1x, 2x and 4x at 0 and 1 bars.
- Block-shuffled surrogates (blocks of 1, 6, 12, 48 bars): shows over what time scale the structure lives.
- Scales: how often direction reverses when price is sampled every 5 minutes to 1 day.
- Tick structure (Deriv serves only the last 24 hours of ticks; a tick-level view needs continuous collection to go further).
- Survival rule, fixed in advance: a baseline is an EXECUTABLE CANDIDATE only if its 95% interval is above zero in both the
  development and validation segments with a normal entry, one bar late, and one bar late with doubled costs.
- The untouched test segment is still never read. A candidate stays Research Only until the untouched test and a forward demo.
- `research.simulate()` gained an optional `delay` and now reports each trade's outcome kind; delay 0 is identical to before.
