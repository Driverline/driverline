# TradeLens AI signal engine: Phase 2 (research infrastructure)

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
