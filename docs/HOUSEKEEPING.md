# Housekeeping — storage pruning

SKCapstone agents accumulate transient files in `~/.skcapstone` (and
`~/.skcomms`) that are never read again after initial processing. Left
unchecked these grow unbounded and bloat a ~15MB profile into hundreds of
megabytes — or, in one real incident, **462k files** that overheated a laptop
(see [Incident background](#incident-background)). Housekeeping reclaims that
space. It is always safe to run.

## What it prunes

| Target          | Path                                                                    | Policy                          |
| --------------- | ----------------------------------------------------------------------- | ------------------------------- |
| `acks`          | `~/.skcomms/acks/`                                                       | age-based — delete > **24h**    |
| `comms_outbox`  | `~/.skcapstone/sync/comms/outbox/<agent>/` (v2)                          | age-based — delete > **48h**    |
| `seed_outbox`   | `~/.skcapstone/sync/outbox/`                                             | count-based — **keep 10** / agent |
| `legacy_comms`  | `~/.skcapstone/comms/outbox/` **and** `~/.skcapstone/agents/<agent>/comms/outbox/` (v1) | age-based — delete `*.skc.json` > **7d**; v1 `*` broadcast dirs removed wholesale |

### Legacy / broadcast sweep (new in 0.13.0)

`legacy_comms` covers the v1 outbox layouts that the v2-only sweep never
reached:

- `~/.skcapstone/comms/outbox/<recipient>/*.skc.json` — the v1 root path.
- `~/.skcapstone/agents/<agent>/comms/outbox/<recipient>/*.skc.json` — the v1
  per-agent path.

Within each outbox it recurses one level into per-recipient subdirs and deletes
envelope files older than 7 days. A recipient subdir whose name is literally
`*` is a v1 `recipient="*"` presence-broadcast artifact — never valid under v2 —
so the **entire directory tree is removed regardless of age** (symlinks are
never followed; the sweep stays inside `~/.skcapstone`). Now-empty recipient and
outbox directories are removed afterward.

## How it runs

- **Hourly daemon loop.** While `skcapstone.service` is running, the daemon
  runs `run_housekeeping()` every hour.
- **Weekly default job.** A standalone scheduler drop-in
  (`~/.skcapstone/config/jobs.d/housekeeping.yaml`, schedule `0 4 * * 0` —
  Sunday 04:00) runs `skcapstone housekeeping` weekly as a **safety net
  decoupled from the daemon**. It is installed automatically on a fresh
  `skcapstone init` (idempotent — an existing user file is never overwritten).
  Manage it with the scheduler:

  ```bash
  skcapstone scheduler list
  skcapstone scheduler run housekeeping     # run now
  skcapstone scheduler disable housekeeping # turn off
  ```

## Running manually

```bash
# Preview what would be deleted (no changes made):
skcapstone housekeeping --dry-run

# Reclaim space now:
skcapstone housekeeping
```

The CLI prints a per-target table (size before, files deleted, space freed) and
a total summary.

## Incident background

On 2026-06-16 a Framework 13 laptop was overheating. Root cause: `~/.skcapstone`
had grown to **462k files**, of which ~256k were stale **v1 broadcast
envelopes**. A v1 `recipient="*"` presence-broadcast was written to a directory
*literally named* `*`, and these lived in the legacy v1 outbox paths
(`comms/outbox/` and `agents/<agent>/comms/outbox/`). The housekeeping at the
time only pruned the v2 path `sync/comms/outbox/` with a 48h TTL and never
reached the legacy paths, so they grew without bound. The `legacy_comms` sweep
and the weekly default job were added to close that gap permanently.
