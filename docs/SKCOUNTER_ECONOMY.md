# SKCounter in the Economy workspace

## Information architecture

Keep SKCounter as the metering subsystem and repository name. Present it in SKDashboard as `Economy > AI Usage` beside `Economy > Autopilot` and `Economy > Joule`.

The three views answer different questions:

- AI Usage: how much model capacity the fleet consumed, where it was consumed, and how complete the measurements are.
- Autopilot: what governed autonomous work cost and how it settled.
- Joule: how sovereign value, balances, levels, minting, and spending moved.

Tokens, USD, and Joules are not interchangeable. The UI must not add them together or imply a conversion rate. A future conversion or budget policy must be explicit, versioned, reviewable, and displayed as a derived projection.

## AI Usage content

The primary view includes:

- Total input, output, cache-read, cache-write, reasoning, and overall tokens.
- Estimated or billed USD cost with pricing revisions.
- Cache ratio and timing coverage.
- Messages, active time, longest continuous activity, and peak concurrency.
- Daily and hourly time series.
- Contribution-style activity history.
- Model, client, provider, node, agent, privacy-safe workspace, privacy-safe session, and approved task breakdowns.
- Harness-reported and gateway-observed lane selection.
- Collector node, principal, SKCounter version, backend, backend version, last report, and freshness.
- Expected, reporting, fresh, delayed, stale, and missing fleet coverage.
- Validation errors that identify an observation file but never echo its content.

Tokscale subscription quota data is intentionally absent. It requires provider account calls and credential handling, so it belongs in separately governed provider connectors rather than the local session scanner.

## Data contract

SKDashboard reads append-only `skcounter.snapshot.v1` JSON observations from `${SKCOUNTER_DATA_DIR}/observations/`. If the environment variable is absent, it reads `<agent-home>/skcounter/observations/`.

The dashboard is read-only. A separate central collector validates CapAuth, transport, replay, schema, and payload size before writing observations. SKDashboard performs defensive validation again and rejects unknown fields, raw-data fields, unsafe links, oversized files, unsupported lanes, and unsupported schemas.

The projection retains only the newest observation for each logical key within one view. Model, daily, hourly, agent, session, and time-metric views can overlap and are never combined. The main total uses only the `models` view. Time series use only `daily` and `hourly` views.

## Privacy-safe drilldown

Central observations never include raw prompts, responses, tool input, tool output, workspace paths, source paths, credentials, or raw session identifiers. Optional workspace and session correlation uses node-scoped HMAC keys. Workspace labels come only from an explicit operator alias map.

## Fleet integration

Set `SKCOUNTER_EXPECTED_NODES` to the comma-separated eligible harness-node set and `SKCOUNTER_EXPECTED_GATEWAY_NODES` to the comma-separated eligible gateway-node set until SKDashboard consumes the authoritative SKCapstone Fleet eligibility projection directly. Each measurement lane computes coverage from its own inventory. Missing nodes are a coverage state, not zero usage.

Collectors run per harness user. A root collector must not scan every home directory. The recommended schedule is every 15 minutes with randomized delay and a local durable outbox for offline delivery.

## Failure display

- `current`: at least one valid observation and no projection errors.
- `degraded`: valid data with rejected observations, or only rejected observations.
- `empty`: no accepted observations and no projection errors.
- `fresh`: last observation is at most 45 minutes old.
- `delayed`: last observation is older than 45 minutes but no more than 24 hours old.
- `stale`: last observation is older than 24 hours.

The UI must show unavailable cost separately from zero cost and missing coverage separately from zero usage.
