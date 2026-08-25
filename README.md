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

This package provides `skdashboard-read-only` for named read-only listeners. The
legacy operator dashboard still has no unit of its own; its deployed unit is
`skcapstone-dashboard.service`, whose ExecStart is
`~/.skenv/bin/skcapstone dashboard --port 7778`; that CLI resolves
`skcapstone.dashboard` to this package through a transparent alias shim (which
lives in `skcapstone`), so routes are byte-identical to the pre-split dashboard.
Full deploy and rollback: [`SOP.md`](SOP.md) section 5.

## Test

```bash
~/.skenv/bin/python -m pytest tests/ -q
```

## Read-only control-plane client and MCP resources

`skdashboard.control_plane_client.ControlPlaneClient` discovers the canonical
same-origin API from `/.well-known/skworld-module.json`, accepts a caller-owned
short-lived bearer, allowlists the frozen V1.1 read and insight-query routes,
and validates every successful response against packaged copies of the
published JSON Schemas. It supports conditional reads, bounded opaque-cursor
pagination, event resume, saved-scope reads, metric-family selection, exact
report snapshots, insight proposals, and evidence-reference extraction.

For development without production state, use
`skdashboard.control_plane_fixture.create_fixture_app()` with
`httpx.ASGITransport`. The fixture is public synthetic, deterministic, and
contains deliberate model abstention rather than live inference.

`skdashboard-control-plane-mcp` publishes fixed read-only MCP resources and one
hash-addressed report template. It publishes no MCP tools, command preview,
authorization, shell, filesystem, connector, or arbitrary endpoint access. The
bearer file must be mode `0600` and is never returned in resource metadata.

```bash
skdashboard-control-plane-mcp \
  --discovery-url https://DASHBOARD/.well-known/skworld-module.json \
  --bearer-file /run/user/$(id -u)/skdashboard-read.cap
```

The frozen contract currently advertises insight query while a deployed
runtime may not yet serve it, and older overview projections may use scope
fields outside the frozen V1.1 schema. The client intentionally fails closed
in either case instead of accepting an unvalidated response.

## Report delivery simulation

`skdashboard.report_delivery.ReportDeliveryService` is an offline,
disabled-by-default development simulation over immutable report snapshots.
Drafts do not create an outbox message. Activation requires an exact report
hash, audience, destination, classification, source-rights reference, purpose,
retention period, redaction profile, unexpired destination verification,
unexpired approval, and a caller-injected policy allow decision for
`skdashboard.reports.deliver.simulate`.

The service stores only bounded delivery metadata, transactional outbox state,
content-free simulation receipts, and append-only audit references in a local
mode `0600` SQLite database. Only the built-in deterministic
`SimulationDestination` is accepted. There is no HTTP, UI, MCP, network,
connector, production destination, account, or deployment surface. Protected
Tenant or Matter reports fail closed and remain in the SKLegal external-action
state machine.

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
