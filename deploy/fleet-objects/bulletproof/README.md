# Bulletproof deploy as fleet objects (CR-7.3)

These are the bulletproof-deploy runbooks (epic `045a8a8f`, the nine
`docs/deploy-plan/*-bulletproof-deploy.md` plans) re-expressed as declarative
**fleet control-plane objects**: `Service` (spec 5.2) and `CronJob` (spec 5.4)
of the SKWorld fleet control plane. No new deploy scripts: the control plane
(`skfleet` + `skcapstone.fleet`) is the deploy tool.

Reference: `skos/docs/specs/2026-07-27-skworld-fleet-control-plane-design.md`
(section 5, resource model) and
`skos/docs/specs/2026-07-27-fleet-phase1-implementation-plan.md`.

## Status: authored + validated, NOT applied

Every object here was validated against the live control-plane model
(`skcapstone.fleet.services.normalize_service_spec`,
`skcapstone.fleet.cron.normalize_cronjob_spec`) and round-trips cleanly
through `store.write_spec` / `read_spec`. They have **not** been applied to
the live `~/.skcapstone/fleet/` tree. Applying is the operator's step:

```
skfleet apply -f deploy/fleet-objects/bulletproof/service/skgateway.json
skfleet apply -f deploy/fleet-objects/bulletproof/cronjob/skmem-reconcile.json
# ... one per object; .158 (control-plane node) first, then soak, then .41.
```

`skfleet apply` bumps the object's `generation` and writes desired state.
Actuation is opt-in per node (`spec.actuate`, report-only by default) and
gated by the freeze switch (`skfleet freeze`); nothing here self-actuates on
commit.

## Revert path (AC2): restore prior spec generation

Every object carries a `spec.revert` field with the same contract:

> Restore prior spec generation: re-apply the previous manifest generation
> (`skfleet apply -f` the prior JSON). `write_spec` bumps `generation` on
> every write, so generation N-1 is the authoritative rollback target.
> Declarative and reversible by spec regeneration; there is no imperative
> rollback script.

To revert a service to its prior state: check out the previous version of its
manifest (git history of this directory is the generation ledger) and
`skfleet apply` it. The generation increments; the *content* returns to the
prior generation. This is the fleet-wide analogue of "restore prior pair"
from the deploy discipline.

## Object inventory

### Services (`service/`, 14) - long-running workloads (spec 5.2)

| Object | Unit | Runtime | Node | Health | Bulletproof conditions |
|---|---|---|---|---|---|
| skcomms | skcomms-api.service | systemd-user | always-on | :9384 | PathHealthy, QueueDrained, OutboxBounded |
| skcapstone-daemon | skcapstone@lumina.service | systemd-user | control-plane | - | MemoryBounded (the .41 OOM cap) |
| skgateway | skgateway.service | systemd-user | always-on | :18780 | UpstreamServing, RegistryPopulated (empty-registry false-green) |
| skgateway-claude-wrapper | claude-code-api.service | systemd-user | control-plane | :18782 | WrapperReachable (the :18782 SPOF) |
| skmemory-daemon | skmemory-daemon.service | systemd-user | always-on | - | EmbedReachable (fail-loud, no NULL embeds) |
| capauth-keystore | capauth-keystore | docker | control-plane | - | RootCustodyVerified |
| skchat-daemon | skchat-daemon.service | systemd-user | always-on | - | restoreBeforeStart (history+keys) |
| skchat-telegram-bridge-lumina | skchat-telegram-bridge@lumina.service | systemd-user | always-on | - | BridgePollFresh (silent-wedge) |
| skchat-piper-tts | piper-tts.service | systemd-user | always-on | :18797 | Ready, CrashLooping |
| skchat-nostr-relay | nostr-relay.service | systemd-user | always-on | :7447 | Ready, CrashLooping |
| skchat-webui-lumina | webui@lumina.service | systemd-user | always-on | - | Ready, CrashLooping |
| skchat-coturn | coturn | docker | always-on | - | Ready, CrashLooping |
| skos-scheduler | skos-scheduler.service | systemd-user | control-plane | - | SchedulerAlive (owns the crons) |
| skingest | skingest.service | systemd-user | always-on | - | SchemaApplied (no start on unmigrated store) |

Standard Service conditions (`Ready`, `Progressing`, `CrashLooping`) apply to
every service and are derived by ServiceController/sknoded; the table lists the
bulletproof-specific conditions each runbook added. `healthCheck` (port) wires
the `Ready` probe. `restoreBeforeStart` and `dependsOn` encode the
restore-before-start ordering theme (fresh daemons must not replicate empty
state fleet-wide).

### CronJobs (`cronjob/`, 12) - scheduled bulletproof jobs (spec 5.4)

| Object | Command | Schedule (window) | Node | Conditions |
|---|---|---|---|---|
| skmem-reconcile | skmemory reconcile --all-agents | @daily (04:15) | always-on | ReconcileFresh (THE HA path) |
| skmem-reindex | skmemory reindex | @daily (04:30) | always-on | MissedRun |
| skmem-pg-dump-offbox | deploy/ops/skmem-pg-backup.sh | @daily (03:15) | always-on | BackupFresh, BackupOffbox |
| skmem-gfs-tarball | deploy/ops/skmem-gfs-tarball.sh | @daily (02:45) | always-on | BackupFresh |
| skmem-health | deploy/ops/skmem-health.sh | @daily | always-on | MissedRun |
| skcapstone-backup-gfs | scripts/backup-gfs.sh | @daily | control-plane | BackupFresh, BackupOffbox |
| skgateway-parity-check | scripts/gateway-parity-check.sh | @daily | control-plane | ParityHeld (.158/.41 drift loud) |
| capauth-custody-doctor | capauth doctor custody | @daily | control-plane | CustodyVerified (root cert/backup/2nd home) |
| skcomms-housekeep | skcomms housekeep --all-agents | @hourly | always-on | ArchiveBounded (the 140k-file freeze) |
| skchat-backup-offbox | deploy/backup-skchat.sh | @daily | always-on | BackupFresh, BackupOffbox |
| skos-morning-brief | skos brief send | @daily (07:15) | control-plane | MissedRun (fail loud, no swallow-and-exit-0) |
| autopilot-daily | skos autopilot run --daily | @daily | control-plane | MissedRun |

The v1 CronJob `schedule` grammar is `@hourly | @daily | @weekly | <N>m | <N>h`
(see `skcapstone.fleet.cron`); it has no wall-clock cron expression yet, so the
intended time is recorded in an informational `spec.window` field. `MissedRun`
is derived by CronController; the extra conditions are the bulletproof
freshness/parity/custody surfaces.

## Deliberate exclusion: skmem-pg

Per design spec 5.2, `skmem-pg` stays OUT of fleet management (it is
local-per-node by the 2026-07-12 replication-drift incident decision). It is
**not** modelled as a Service here. Its liveness is a node-level health
condition only (`skcapstone.fleet.conditions.probe_conditions`, a TCP probe on
:5432), never an actuation target. The `skmemory-daemon` service records this
as a `dependsOn` note; the memory HA mechanism is the `skmem-reconcile`
CronJob (rebuild-from-flat), not fleet failover.

## Provenance

Each object's `spec.source` points at the runbook it re-expresses. The 44
decompose children of epic `045a8a8f` were archived against this story
(reversibly, into `coordination/archive/`); the archive list is in the CR-7.3
run note under `~/clawd/docs/handoffs/`.
