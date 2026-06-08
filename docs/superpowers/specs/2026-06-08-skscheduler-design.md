# skscheduler — Unified Fleet Job Scheduler

**Date:** 2026-06-08
**Status:** Design approved, pending spec review
**Driver incident/problem:** `prb-7810b08e` (service_health multi-write Syncthing conflicts), `inc-455b1a64`
**Immediate need:** a daily GTD inbox-triage agent running on .41

## Problem

Scheduling across the fleet is fragmented across four mechanisms with no single
place to define, run, or observe jobs:

1. **skcapstone `TaskScheduler`** (`scheduled_tasks.py`) — interval-only Python
   callbacks (heartbeat, backend_reprobe, memory_promotion_sweep,
   dreaming_reflection). Runs inside the skcapstone daemon, which is **inactive
   on .41**.
2. **Legacy user crontab** — 5 jobs pointing at a stale pre-skcapstone path
   (`~/dkloud.douno.it/.../docs/memory/memory/scripts/`).
3. **systemd user timers** — `skcapstone-context` (active), `skcomm-heartbeat`
   (dead), `direnv-backup`.
4. **Claude Code crons** — separate scheduled-agent system.

Consequences observed: the GTD inbox is never processed (capture is automated,
clarify is not — no job runs it); ITIL problem→GTD-project lifecycle leaks
(stale projects at 77–82 days); and — the root incident — `service_health`
running on multiple nodes does read-modify-write on the **same Syncthing-synced
incident files**, producing recurring `.sync-conflict-*` files.

## Goals

- **One declarative registry** for all recurring jobs, synced across the fleet.
- **One management surface** (`skcapstone scheduler` CLI) to list/run/observe.
- **Cron-style time-of-day** schedules in addition to intervals.
- **Run agent-judgment jobs** (GTD triage, reflections), not just Python callbacks.
- **Per-job node affinity** so a job runs on exactly the intended node(s) —
  directly preventing the multi-writer conflict class.
- **The scheduler must never become a sync-conflict source itself.**

## Non-goals

- Migrating Claude Code crons into skscheduler (different system; documented only).
- A distributed consensus/leader-election layer. Affinity is declared, not elected.
- Replacing systemd as the process supervisor (it keeps the daemon alive).

## Decisions (locked during brainstorming)

1. **Extend the existing `TaskScheduler`** — do not build parallel infrastructure.
2. **Agent jobs execute via headless Claude Code:** `claude -p --agent <name> "<prompt>"`.
3. **Per-node scheduler + per-job affinity:** every node runs its own scheduler
   thread reading the same synced `jobs.yaml`; each node fires only jobs whose
   affinity includes it.

## Architecture

```
~/.skcapstone/config/jobs.yaml      # Syncthing-synced — the single registry
        │  (read by every node's daemon)
        ▼
skcapstone daemon (systemd user service, per node)
        │
        ▼
TaskScheduler (extended)  ── tick loop ──► due? (cron|interval) AND node in affinity
        │                                         │
        │                                         ▼
        │                                    JobRunner.dispatch(type)
        │                                  ┌───────┼─────────┐
        │                               python   shell     agent
        │                              callback  subprocess  claude -p --agent
        ▼
node-local state  ~/.skcapstone/scheduler/<hostname>/state.json   (NEVER synced)
node-local logs   ~/.skcapstone/scheduler/<hostname>/logs/<job>-<ts>.log
```

### Config schema (`jobs.yaml`)

```yaml
jobs:
  gtd-inbox-triage:
    schedule: "0 6 * * *"     # cron expression  (mutually exclusive with `every`)
    # every: 300s             # interval form, for high-frequency mechanical jobs
    type: agent               # agent | shell | python
    nodes: [".41"]            # affinity: `all` or a list of host aliases
    agent: lumina             # (agent type only) which agent to run as
    prompt: "..."             # (agent type) the task prompt
    command: "..."            # (shell type) the command line
    callback: "module:fn"     # (python type) dotted path to a registered callback
    timeout: 900              # seconds; hard kill
    enabled: true
```

- **Node identity / alias map:** resolve `.41 → cbrd21-laptop12thgenintelcore`
  (and peers) from existing host/identity config rather than hardcoding. A job
  runs on this host iff `nodes == all` or this host's alias ∈ `nodes`.
- **Built-in Python tasks** remain registered in code for back-compat; `jobs.yaml`
  adds/overrides and is where ops-level jobs live.

### Job types

| type | runner | use |
|------|--------|-----|
| `python` | in-process callback (existing) | heartbeat, memory sweep, reprobe |
| `shell`  | `subprocess` of a command | legacy memory scripts, context regen |
| `agent`  | `claude -p --agent <name> "<prompt>"` | GTD triage, reflections (judgment) |

## Execution, observability & conflict-safety

- **Agent jobs:** subprocess with hard `timeout`; stdout/stderr captured to the
  per-run log. Exit code → success/error.
- **State is node-local, never synced** — under `~/.skcapstone/scheduler/<hostname>/`,
  added to `.stignore`. This guarantees the scheduler cannot create the very
  conflicts it exists to help eliminate.
- **Overlap guard:** per-job, per-node lockfile; a fire is skipped if the prior
  run is still active (no piling-up agents).
- **Error isolation:** a failing job never crashes the loop; it increments
  `error_count`, records `last_error`, and continues.
- **Misfire/catch-up:** if the daemon was down across a cron time, the job runs
  once on next start — not once per missed slot.

## CLI

```
skcapstone scheduler list                 # all jobs, schedule, affinity, enabled
skcapstone scheduler status [--json]      # last run / status / counts (this node)
skcapstone scheduler run <job>            # fire now (ignores schedule, respects affinity)
skcapstone scheduler enable|disable <job>
skcapstone scheduler logs <job> [--tail]
```

## Migration

- **Legacy crontab (5 jobs):** convert to `shell` jobs in `jobs.yaml` **after
  confirming each is still wanted** — the `~/dkloud.douno.it/...` path predates
  skcapstone and may be dead. Remove from crontab once migrated.
- **systemd timers:** fold `skcapstone-context` in as a `shell`/`python` job;
  retire the dead `skcomm-heartbeat`. Keep `direnv-backup` out of scope.
- **`service_health`:** declare `nodes: all` (each node probes its own localhost),
  but its *incident writes* are pinned/serialized per the `prb-7810b08e` fix —
  closing the original conflict loop.

## Error handling summary

- Config parse error → log, keep last-good config, surface in `scheduler status`.
- Unknown node alias → job skipped on this node with a warning.
- Agent subprocess timeout/non-zero → recorded as error, logged, loop continues.
- Lock contention → skip-with-note (not an error).

## Testing (TDD at build)

- Cron + interval due-calculation (incl. catch-up/misfire).
- Node-affinity filtering (alias resolution; `all`).
- Job-type dispatch with mocked subprocess for `shell`/`agent`.
- Node-local state persistence + `.stignore` placement.
- Overlap-guard lockfile behavior.

## Rollout phases

1. Config loader + cron support + affinity filtering (no new job types yet).
2. `JobRunner` with `shell` + `agent` types; node-local state/logs; overlap guard.
3. `skcapstone scheduler` CLI.
4. Activate skcapstone daemon (systemd user service) on .41.
5. Register `gtd-inbox-triage` (the driver) in `jobs.yaml`, affinity `.41`.
6. Migrate legacy crontab + timers (after per-job confirmation).

## The immediate driver job

```yaml
gtd-inbox-triage:
  schedule: "0 6 * * *"
  type: agent
  nodes: [".41"]
  agent: lumina
  prompt: >
    Triage the GTD inbox: for each item, clarify into next-action / project /
    someday-maybe or archive noise; move resolved-ITIL items to done; surface
    stale projects. Use the gtd_* and itil_* MCP tools. Keep it concise.
  timeout: 900
  enabled: true
```

## Resolved decisions (post-review)

- **Cron parsing:** use `croniter` as a dependency (battle-tested; avoids a
  hand-rolled parser in a fleet-synced package).
- **`scheduler status` scope:** strictly per-node for v1. A read-only aggregate
  fleet view (reading peers' node-local state via the synced tree) is deferred
  to a later iteration.
