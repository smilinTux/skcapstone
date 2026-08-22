# skdashboard

The SKWorld operator dashboard (coord board + ITIL + kanban + CMDB), extracted
from `skcapstone` (CR-4.3). Serves the `:7778` web UI + JSON API, bound to
`127.0.0.1` only. **Maturity tier: `T0 - N/A (no key material)`**, a non-crypto
repo; the authorization decision belongs to capauth, see
[`SECURITY.md`](SECURITY.md).

Docs: [`SOP.md`](SOP.md) (run it, deploy it, debug it) ·
[`SECURITY.md`](SECURITY.md) (what is actually enforced) ·
[`CONTRIBUTING.md`](CONTRIBUTING.md) · [`CHANGELOG.md`](CHANGELOG.md) ·
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)

## Dependency direction

Coordination access goes through **skcoord** directly (`skcoord.card`,
`skcoord.card_store`, `skcoord.coordination`, `skcoord.itil`, `skcoord.cmdb`).

The CMDB operator page at `/cmdb` reads the canonical event-sourced inventory
and checksum-verified reconciliation artifacts. It shows fleet coverage,
collector completeness, stale or unreachable evidence, reconciliation history,
CI provenance and impact, linked ITIL records, and bounded search filters.
Discovery is preview-first. The plan endpoint never writes, and apply remains
behind the dashboard capability gate.
There are no `skcapstone.coordination` / `skcapstone.card_store` imports (CI grep
gate). The richer agent / runtime / doctor / trust / model panels reach back into
`skcapstone` at runtime via lazy imports, so `skdashboard` depends on both but has
no import-time cycle (the dashboard is launched on demand, after skcapstone is up).

## Launch

This package has **no console script and no unit of its own**. The deployed unit
is `skcapstone-dashboard.service`, whose ExecStart is
`~/.skenv/bin/skcapstone dashboard --port 7778`; that CLI resolves
`skcapstone.dashboard` to this package through a transparent alias shim (which
lives in `skcapstone`), so routes are byte-identical to the pre-split dashboard.
Full deploy and rollback: [`SOP.md`](SOP.md) section 5.

## Test

```bash
~/.skenv/bin/python -m pytest tests/ -q
```

## ATLAS operator cockpit

`/cockpit` includes a read-only operator plane backed by
`GET /api/operator/overview`. It projects typed conditions and evidence age,
the fleet freeze, action lifecycle and verification definitions, execution
cooldowns/circuits, watchdog freshness, CMDB scope/completeness, and skbrain
health/citation counts. Missing or malformed evidence is shown as unknown; an
unreadable freeze source is shown as frozen. Rendering never invokes ATLAS.

Inputs default below `~/.skcapstone/fleet/atlas`. Staged deployments may use
`SKFLEET_ROOT`, `SKATLAS_ROOT`, `SKATLAS_BRIEF_JSON`,
`SKATLAS_ACTION_LEDGER`, `SKATLAS_CMDB_STATUS`, `SK_WATCHDOG_DIR`, and
`SKBRAIN_OPERATOR_HEALTH` to select immutable projection artifacts.
