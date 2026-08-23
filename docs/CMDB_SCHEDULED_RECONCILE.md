# Scheduled CMDB reconciliation

SKCapstone registers one `cmdb-reconcile` scheduler tick on `chiap04`. The
callback also requires the active agent to be `jarvis` and acquires a
node-local application lease. These independent gates prevent two daemon
instances or a manual overlapping tick from applying the same fleet pass.

The bundled job checks every 60 seconds. Actual discovery cadence comes from
`~/.skcapstone/config/cmdb-reconcile.json`. If that file is missing or has
`enabled` set to `false`, the callback exits without scanning or changing CMDB
data.

## Configuration

Create the file only after target scope and opaque credential references have
been reviewed:

```json
{
  "schema": "skcoord.cmdb.reconcile-job-config/v1",
  "enabled": false,
  "owner_node": "chiap04",
  "agent": "jarvis",
  "cadence_seconds": 900,
  "targets": ["chiap04"],
  "credential_refs": {"chiap04": "skvault://fleet/chiap04"},
  "global_concurrency": 4,
  "per_host_concurrency": 1,
  "timeout_seconds": 180,
  "failure_budget": 0,
  "retry_count": 2,
  "retry_backoff_seconds": 30,
  "retention_runs": 96,
  "stale_grace_runs": 3,
  "drift_alert_runs": 2,
  "failure_alert_runs": 3,
  "apply_safe_observations": true
}
```

Every enabled target must have exactly one `skvault://` reference. The runtime
resolves protected SSH file metadata through SKVault. Inline passwords,
private keys, and ambient SSH configuration are not accepted.

## Runtime behavior

Each tick checks configured cadence, node affinity, active agent, and the
application lease. It scans the exact configured targets with bounded global
and per-host concurrency. Retries use configurable linear backoff. A partial
scan may apply safe positive observations but never advances missing-CI
counters or retirement candidates.

Every attempted run writes a checksummed artifact under
`cmdb/reconcile-runs/`. It includes coverage, collector failures, duration,
code and config versions, reconciliation changes, drift, lifecycle preview,
attempt count, outcome, and linked ITIL incidents. The existing `cmdb status`
and dashboard readers consume these artifacts. Old artifact and checksum pairs
are pruned only after a new artifact is durable.

High-severity drift creates an incident immediately. Other drift and collector
failures must remain present for their configured number of consecutive runs.
Open incidents are deduplicated by affected CI and retain the evidence path.

## Enable, disable, and rollback

Start with `enabled: false`, run the governed shadow and rollback drills, then
set `enabled: true` only inside the approved change window. Confirm the job is
visible with:

```bash
skcapstone scheduler list
skcapstone scheduler status --json
skcapstone cmdb status --json
```

To stop future scans, set `enabled` to `false` or run:

```bash
skcapstone scheduler disable cmdb-reconcile
```

Disabling does not alter existing CMDB data or remove retained evidence. A
rollback must not delete or rewrite append-only CI events. If a prior run wrote
an incorrect status, append the corrected status with the bad run artifact
linked in the note.
