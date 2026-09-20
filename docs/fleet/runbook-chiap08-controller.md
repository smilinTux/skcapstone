# Runbook: orchestrating the fleet FROM chiap08

`chiap08` is registered `role=control` (`~/.skcapstone/fleet/objects/node/node-chiap08.json`)
and is Chef's manual controller box for the **chi** estate. This runbook is
the documented entrypoint for that role: what chiap08 can reach, what already
works, and the one rule that made today's (2026-09-19) deploy succeed where an
earlier attempt from `noroc2027` would have failed.

## 🔴 The estate boundary rule — read this first

**A controller must run inside the estate it controls.** `nor` (`noroc2027`,
`192.168.0.41`) and `chi` (`chiap01-04`, `chiap08`, `ziowk01-wsl`) are
disjoint Syncthing shares with separate, non-overlapping CardStores. Fleet
readiness gates read a **local file**:
`~/.skcapstone/fleet/status/node-<host>/readiness/verdict.json`
(`src/skcapstone/fleet/staged_rollout.py::_readiness_verdict`), synced to that
file's home estate only.

Run `skfleet rollout` (or anything that calls `staged_rollout.execute_rollout`)
from `noroc2027` and every chi verdict file is simply absent on that box. The
gate does not know the difference between "this node is unhealthy" and "this
node's estate never sent me a verdict" — both come back as **MISSING**, and
the rollout halts on the first node exactly as if it had failed a real health
check. That is not a fault in the node; it is a fault in where the controller
was run. A deploy attempted from `noroc2027` against chi hosts today halted on
host one for this exact reason. The deploy that succeeded was run from a chi
host (`chiap08`) instead.

**Concretely:**
- Estate-local paths: `~/.skcapstone/fleet/status/**`, `~/.skcapstone/fleet/objects/**`,
  `~/.skcapstone/coordination/**` — all Syncthing-scoped to one estate.
- A `MISSING` (not `false`) readiness verdict when you know the target node is
  actually up is the signature of this mistake: check which estate you are
  running from before treating it as a node problem.
- Rule of thumb: to orchestrate chi, SSH to a chi host (`chiap08` is the
  standing one) and run the command there. Do not run fleet commands against
  chi from `noroc2027`, and do not run them against nor from a chi host.

## What chiap08 can reach today (verified 2026-09-19)

SSH, passwordless, from chiap08:

| host | reachable | role |
|---|---|---|
| chiap01 | yes | worker |
| chiap02 | yes | worker |
| chiap03 | yes | worker |
| chiap04 | yes | worker |
| ziowk01-wsl | yes | builder-standby (`node-ziowk01`) |

CLIs on chiap08's PATH:

| CLI | present | version |
|---|---|---|
| `skcapstone` | yes (`~/.skenv/bin/skcapstone`) | `0.15.168.dev266+g0c8dcd6b` |
| `skfleet` | yes (`~/.skenv/bin/skfleet`) | same package; `skfleet` is the fleet subcommand's own entrypoint, identical surface to `skcapstone fleet` |
| `skcoord` | **no such binary** | use `skcapstone coord ...` instead — there is no separate `skcoord` executable |
| `pi` | yes (`~/.npm-global/bin/pi`) | `0.84.4` |
| `codex` | yes (`~/.npm-global/bin/codex`) | `codex-cli 0.154.0` |

The installed `skcapstone` package is an editable install from
`~/work/skcapstone`, currently on `main` at `0c8dcd6b` (clean tree) — this
matches the fleet's known-good sha. Note `origin/main` has moved ahead of that
sha as of this writing; the fleet is intentionally pinned, not behind by
accident.

Chi board view from chiap08 (`skfleet nodes`) sees all chi nodes plus
`ziowk01`; readiness verdicts for all five known-good hosts
(chiap01-04, chiap08) read `ready: true`, checked within the last
`skfleet-readiness.timer` cycle. `skfleet nodes`' own Ready/Pending/Dead phase
column reflects live heartbeat cadence (`beats/`), which is a separate signal
from the readiness verdict used to gate rollout — a stale heartbeat does not
by itself mean the readiness gate would fail; check `skfleet describe` /
the verdict file directly if the two disagree.

## The orchestration entrypoint

Nothing new was built for this. `skfleet rollout` already wraps
`staged_rollout.execute_rollout` and is dry-run by default:

```bash
ssh chiap08
cd ~/work/skcapstone   # or pass --repo-root explicitly
skfleet rollout --node chiap01 --node chiap02 --node chiap03 --node chiap04 --json
```

This previews the plan only — no deploy, no gate call, no filesystem write,
for any node — and is safe to run at any time to sanity-check the fleet from
chiap08. Verified working 2026-09-19: all four nodes returned
`dry_run: true`, `ready: false` (dry-run never calls the real gate), nothing
halted.

To actually roll: add `--apply`. The command halts at the first node that
fails to deploy or fails its gate; every later node is left untouched. See
`skfleet rollout --help` for the full contract (repo-root, remote-repo-root,
`--strict` exit code, `--json`).

Other first-class chiap08 entrypoints, no new tooling needed:
- `skfleet nodes` / `skfleet describe node <name>` — board + per-node object.
- `skfleet node drift --json` — this node's own drift vs. expected units
  (remote nodes are graded over SSH by the rollout gate itself, not by
  running this command against them from chiap08 — see `staged_rollout.py`'s
  own comment on why a local checkout cannot grade a remote node in-process).
- `skcapstone coord board` / `skcapstone coord briefing` — the coordination
  board; `AGENTS.md` at the repo root is Step 1 for any fresh agent and
  already documents this.
- `skfleet rollback --node ... [--apply]` — same staged, halt-on-first-failure
  mechanism, in reverse, using each node's own recorded previous manifest.

Do NOT build a parallel script that re-implements any of the above; all of it
already runs from chiap08 today.

## `pi` and `codex` on chiap08

**`pi` 0.84.4** — configured, working, pointed at the chi gateway.
`~/.pi/agent/models.json` has provider `skgateway`, `baseUrl`
`http://chiap01:18790/v1` — the **chi** gateway
(`SKFLEET_GATEWAY_URL=http://chiap01:18790`, confirmed against
`skfleet-rotate.service`'s effective systemd environment), not
`localhost:18780`. `pi auth check --provider skgateway --model sk-s --json`
returns `{"status": "ready", "provider": "skgateway", "authType": "api_key"}`.
`pi` is a live worker mechanism here already: dozens of
`~/.skcapstone/fleet/workspaces/pi-<seat>-chiap08-<id>/` directories exist
from prior fleet work (seraph, codex-review seats etc.), so `pi` can and does
drive fleet work today, through the model catalog it reads from
`~/.pi/agent/models.json` — **not** from `SKFLEET_GATEWAY_URL` directly; that
env var is what feeds the catalog sync script (below), and confusing the two
is a known source of drift.

One live gap: the model catalog's own sync marker
(`~/.pi/agent/models.json` → `skfleet_catalog_sync.invalidated`) currently
reads `true`, meaning the gateway's advertised inventory or revision has
changed since the cache was last reconciled. The reconciler that clears this,
`skfleet-pi-model-catalog.py`
(`~/.skenv/bin/skfleet-pi-model-catalog.py`), is installed but **has no
systemd timer or cron entry anywhere on chiap08** — `systemctl --user
list-units --all` and `crontab -l` both come up empty for it. It is a
manual-only step today. See Gaps below.

**`codex` 0.154.0** — configured and working, but **not** pointed at the chi
gateway and not itself a fleet-routing tool. `~/.codex/config.toml` has
`model = "gpt-5.6-sol"` with MCP servers (`skmemory`, `skcapstone`, `skcomms`,
`skchat`) wired for agent `jarvis`. This is a general-purpose coding-agent
seat (heavy prior use: multi-GB history/session DBs, dozens of trusted
project paths including `~/work/skcapstone` itself and several
`~/.skcapstone/fleet/workspaces/...` seats) that talks to its own model
provider directly, not through `SKFLEET_GATEWAY_URL`. It can drive fleet
*work* the same way any coding agent can — editing this repo, running
`skfleet`/`skcapstone coord` commands as its tools — but it does not route
inference through the chi gateway itself the way `pi` does. Treat `codex` on
chiap08 as "a capable agent seat that happens to live here", not as a
gateway-routed fleet primitive.

## Gaps (honest accounting)

1. **`pi`'s model-catalog reconciler is unwired.**
   `skfleet-pi-model-catalog.py` exists, reads `SKFLEET_GATEWAY_URL`, and
   clears `invalidated`, but nothing calls it periodically on chiap08.
   Size: small — one `.timer` + `.service` pair modeled on
   `skfleet-readiness.timer` (same directory, same pattern already proven on
   this box). Not built here because this task is documentation/discovery,
   not a new deploy, and "no deploy" was an explicit boundary.
2. **No committed doc previously stated the estate-boundary rule.** This
   runbook is the fix; linked from `AGENTS.md` so a fresh agent sees it at
   Step 1 rather than rediscovering it by halting a rollout.
3. **`skcoord` does not exist as a binary** despite being a natural guess;
   worth a one-line shim or alias if it keeps tripping people up, but not
   done here — `skcapstone coord` is a two-token muscle-memory fix, not a
   missing capability.
4. **Interactive chiap08 shells do not inherit `SKFLEET_GATEWAY_URL`** (it is
   only set via `Environment=` on specific systemd units, e.g.
   `skfleet-rotate.service`). This did not block anything tested here
   (`skfleet rollout`, `skfleet node drift` both worked unset), but any
   future command that does need it (like the catalog reconciler above) will
   require exporting it by hand or wiring a login-shell default. Small,
   deliberately not done here to avoid changing shell init files on a shared
   box outside this task's scope.

## What was NOT touched

No fleet actuation, no `--apply` rollout, no service restart, no config
change to `pi`, `codex`, or any systemd unit. `pid 400887`,
`sk-parallel-wave2-*`/`sklegal-*` tmux sessions, and `SKFLEET_WEDGE_MODE`
were left exactly as found. Every command run for this runbook was read-only
or an explicit dry run.
