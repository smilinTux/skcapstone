# SKCP-30 forecast engine qualification

Date: 2026-08-24

Card: `169028ce`

Base main: `c8a4048738ce3d1d4bfceebf4625476fea26792c`

## Delivered slice

The standalone `skdashboard.forecast` module builds versioned, read-only
aggregate schedule forecast artifacts. It uses Python standard-library seeded
bootstrap sampling and does not modify the frozen schedule v1.0.0 contracts,
owner records, HTTP routes, or browser UI.

The engine provides:

- explicit P50, P85, and P95 completion ranges measured in aggregate periods;
- fixed seed, method, cohort, scope, history window, sample, assumptions, and
  exclusions for reproducibility;
- immutable dependency delay, remaining work, and aggregate service capacity
  sensitivity scenarios bound to the exact SKCP-20A projection ID, version,
  `projection_hash` in `sha256:<64>` form, and separate deterministic
  `input_hash` in the same hash form;
- affected dependency paths computed from a topology capped at 256 paths and
  64 items per path, rather than accepted as scenario claims;
- milestone confidence without converting a range into a promised date;
- leakage-free rolling-origin backtests with coverage, P95 misses, throughput
  drift, zero-throughput training abstention, and material drift abstention;
- automatic exclusion of migrated administrative completions and mixed-clock
  periods before canonical overlap validation;
- mixed-cadence abstention so 1, 7, 30, and 90 day counts cannot be pooled;
- separately modeled blocked delay so a 1000-period block does not consume the
  bounded delivery simulation horizon; and
- explicit method discrimination from date-based critical-path calculation.

Every artifact states `individual_ranking_prohibited: true`,
`writes_owner_records: false`, and `calculation_owner: deterministic_engine`
where calculation is performed. No person, assignee, individual-capacity, or
productivity field is accepted or emitted.

## Acceptance evidence

1. Forecast metadata and quantiles are asserted in
   `test_forecast_is_reproducible_truthful_and_range_only`.
2. Exact projection binding, bounded topology derived paths, sensitivity,
   source immutability, unknown-item rejection, and zero-capacity abstention
   are asserted in the dependency tests.
3. Calibration coverage, misses, drift, zero-training abstention, material
   drift abstention, insufficient-outcome abstention, and non-leakage are
   asserted in the rolling backtest tests.
4. Excluded administrative overlap, mixed-clock exclusion, and mixed-cadence
   abstention are asserted explicitly.
5. A 1000-period blocked delay is asserted separately from delivery periods.
6. Recursive key checks assert that forecast artifacts contain no individual
   ranking fields.

## Independent review repair

The first independent review failed on six adversarial gaps. This candidate
repairs each gap without changing the frozen schedule contract, UI, API,
owner records, or deployment:

1. Non-canonical migrated and administrative periods are filtered before
   canonical overlap validation.
2. Canonical periods must have one comparable duration or the forecast and
   backtest return typed abstention. Backtest cadence abstention bypasses
   drift calculation, so incomparable periods cannot overwrite its reason.
3. Every scenario carries the exact SKCP-20A schedule projection binding and
   derives affected paths from the bounded projection topology. The binding
   uses the frozen `projection_id`, `projection_version`, and prefixed
   `projection_hash` field names and representation.
4. Zero-throughput rolling training returns typed abstention.
5. Throughput drift outside the reciprocal approved factor, 0.5 through 2.0
   by default, returns typed abstention.
6. Blocked delay is added after a separately bounded delivery simulation, so
   a 1000-period delay is represented rather than exhausting that horizon.

## Qualification

- Focused forecast tests: `16 passed in 0.04s`.
- Full repository suite: `462 passed, 6 warnings in 28.61s`.
- Ruff check over `src` and `tests`: passed.
- Ruff format check over changed Python and tests: passed.
- Git whitespace check: passed.

The six warnings are pre-existing `jsonschema.RefResolver` deprecation warnings.

## Frozen rereview candidate

Base commit: `c8a4048738ce3d1d4bfceebf4625476fea26792c`

- `src/skdashboard/forecast.py` sha256:
  `9cf042ced38c52cd5676a4b3c215435d77c37fc66c71165ad042abb08ed34301`
- `tests/test_forecast.py` sha256:
  `6f43daa22607904cb483f2254aeb7035c097547822ba405f349375c433156f87`
- Ordered filename and byte payload sha256 for those two implementation files:
  `04fba86d830b6d79b9426b831e74a9758ca8eee51a768d8536eea0dda06e3ba8`

The evidence file is linked separately after its final hash is calculated.

## Limits and rollback

This slice creates no production history provider, forecast API, UI, model
recommendation, deployment, external action, owner mutation, schedule date, or
individual capacity estimate. The caller must supply an approved aggregate
cohort and canonical periods. Date-based critical path remains a separate
method and is never blended into throughput quantiles.

Rollback is reverting or deleting the additive module, tests, and evidence
before a future consumer adopts them. No migration or runtime state is created.
