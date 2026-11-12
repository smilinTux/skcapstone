# Worker RPC Control: live stream out, guided injection in

- **Date:** 2026-09-19
- **Status:** Draft, farmed to fleet cards (read-only half); supervised change gated (injection half)
- **Related:** #806 (work-router coherence), #811/#812 (`deployment_manifest.PER_HOST_ARTIFACTS`), PR #822 (`scripts/fleet/skfleet-worker-stream.py`)

## What Chef asked for

Two capabilities, in his words:

1. "ultimately, i'd love to have the output streamed to our dashboard, or a view into it"
2. "if we stream the tmux session then we could add prompts to the session if it gets stuck"

Capability 1 is buildable today with zero changes to the worker launch path.
Capability 2 is real and worth building, but tmux is the wrong mechanism and the
right mechanism has one unresolved unknown. Per Chef's explicit instruction,
this spec assumes mid-run injection works and marks every decision that depends
on that assumption so revision is cheap. Section "If injection proves
impossible" names exactly what dies and what survives.

## Why not tmux

Chef reached for tmux because it is the familiar way to get an injectable
terminal. His instinct about the capability is right; the mechanism is wrong,
for measured reasons:

- The current wrapper (`~/.local/bin/skfleet-worker-wrapper.py`) launches
  workers with `stdout` redirected to a plain file and **`stdin=/dev/null`**
  (measured on a live chiap03 worker: `tty=?`, `stdin=/dev/null`). There is no
  terminal to attach to and no input channel for `send-keys` to feed. Wrapping
  the worker in tmux to create one would reintroduce exactly what the fleet
  migrated away from.
- The fleet already paid the tmux tax: 33 orphaned sessions cleaned up on
  2026-09-19, one 12.8 days old, and a digest that still counts
  `codex-auto-*` tmux sessions and reads 0 while 8 workers run.
- systemd gives supervision, restart policy, and cgroup limits. Those must not
  be lost for the sake of an input channel.

The right mechanism is `pi --mode rpc`. Probed on chiap08
(`echo "" | pi --mode rpc --print --no-session --no-tools`): it emits
structured JSON events on stdout and actively parses JSON commands from stdin
(`"Failed to parse command: Unexpected end of JSON input"` on empty input).
Bidirectional, structured, no pty required.

## The one unknown, and how this spec handles it

`--print` means "process prompt and exit". What is verified: rpc mode reads
commands from stdin and writes structured events to stdout. What is NOT
verified: whether a long-running rpc session accepts an additional instruction
MID-RUN, or only at start. Opus is probing the real rpc vocabulary in
parallel; this spec does not duplicate that probe.

Design consequence: everything in this spec is split into a **read-only half**
(stands on its own, farmable now, no dependency on the unknown) and an
**injection half** (gated on the probe result, with a designed fallback that
preserves the user-visible capability even if mid-run injection is
impossible).

## Capability 1: the live stream (read-only, not gated)

### The signal that already exists

`pi` writes an NDJSON session file per worker at
`~/.pi/agent/sessions/<workspace-slug>/<ts>_<uuid>.jsonl`; the slug contains
the card id. Events are `{type, id, parentId, timestamp, message}` with roles
`assistant`/`toolResult`, appended continuously. Measured on all five chi
hosts on 2026-09-19: every live worker had written within seconds. This
requires **no change to the wrapper or the launch path**.

### Projection, never raw

Sessions run 45 to 57 MB with a ~14 KB average event. No consumer may stream
them raw. The canonical projection, one row per event:

```json
{"ts": "...", "host": "chiap03", "card": "25ab78c6", "role": "assistant", "tool": "bash", "preview": "first 140 chars of text..."}
```

Cost: a projected row is roughly 200 to 300 bytes against a ~14 KB source
event, a 50 to 70x reduction. A follower additionally starts at the current
tail of each session file, never replaying the multi-MB history, so what the
dashboard actually consumes is the live trickle of new events (per-worker on
the order of a few rows per minute), not the archive. Snapshot mode reads
only file metadata plus a line count.

The reference implementation is `scripts/fleet/skfleet-worker-stream.py`
(PR #822), productized from a prototype verified on all five chi hosts. Two
defects were found live and their fixes are load-bearing; any reimplementation
MUST preserve them:

1. **Stale-session guard.** A card's session glob matches sessions from
   PREVIOUS runs of the same card. Taking the newest unconditionally reported
   a worker as silent for 8.4 days when it had just started. Sessions older
   than the unit's `ActiveEnterTimestamp` (minus small skew) are refused.
2. **Active-only unit filter.** `systemctl --user list-units` without a state
   filter includes FAILED units. Counting one made an 8.5-day-dead unit
   (`skfleet-worker-glm-l-25ab78c6-repair`, MainPID=0, failed since
   2026-09-10) read as a live silent worker. Filter `--state=active`; report
   failed units separately as cruft, never as activity.

### The liveness signal, and the trap this spec exists to record

**Worker liveness = mtime of the current run's pi session file.** Nothing
else.

**Workspace file mtime is WRONG and a PR built on it was opened and had to be
closed on 2026-09-19.** Measured that day: workspace mtime showed 5 of 6
long-running workers "silent" for 111 to 276 minutes. All 6 were working.
Fleet workers do most of their work through tool calls that READ (git, coord
status, file reads), so a fully engaged worker leaves its workspace untouched
for hours. A stall detector built on workspace mtime fires on healthy
workers. Do not rebuild it.

The other look-alike signals are also wrong: systemd `active` says nothing
about progress (a wedged process is still active); CPU reads 0% for both a
wedged worker and one between tool calls; the wrapper's stdout log is a plain
file that stays 0 bytes until exit.

**Rule, generalized: any progress proxy MUST be validated against a
known-good control before it ships.** Concretely: run the proposed signal
against at least one worker known to be working (verified by reading its
session tail) and one known to be dead (a failed unit), and show it separates
them. The workspace-mtime PR would have died in five minutes under this test.

### Architecture of the read-only half

```
per host:  pi session NDJSON  ->  skfleet-worker-stream.py  ->  projected rows (stdout NDJSON)
chiap08:   per-host projections  ->  aggregator  ->  merged feed  ->  dashboard view
```

- Per-host streamer: PR #822, snapshot and `--follow` modes.
- Aggregator: runs on chiap08 (the orchestration entrypoint, PR #810), pulls
  the five per-host projections, merges into one time-ordered feed. Projected
  rows only cross the network; raw sessions never leave their host.
- Dashboard view: renders per-worker card/host/last-event-age/last-tool/
  preview, plus a stall flag from the honest liveness signal, plus failed
  units as cruft rows.
- Digest fix: the fleet digest replaces its tmux `codex-auto-*` count
  (currently 0 while 8 workers run) with the systemd-active-unit count from
  the same liveness module, so there is exactly one source of truth.

## Capability 2: injection (gated on the mid-run unknown)

### Mechanism, assuming the probe succeeds

The wrapper's launch of `pi` changes from `stdin=/dev/null` to a pipe (or
socket) speaking rpc-mode JSON commands, with `pi` invoked in `--mode rpc`.
An injection daemon-side consumer reads guidance requests from a per-card
queue (below) and writes them as rpc commands to the worker's stdin. This
wrapper change is the ONLY part of the whole design that touches the worker
launch path, and it is explicitly on the supervised side of the boundary
(next section).

### The guidance-request queue (farmable now, mechanism-agnostic)

The producer side of injection does not depend on the unknown and is safe to
build immediately:

- `skcapstone fleet nudge <card-id> "<text>"` appends a guidance request
  `{ts, author, host, card, text}` to a per-card queue file under
  `~/.skcapstone/fleet/guidance/<card-id>.ndjson`, and records the same
  request as card evidence through the coord CLI write boundary (never by
  hand-appending to CardStore JSONL).
- The queue is deliberately mechanism-agnostic: its consumer is EITHER the
  mid-run rpc injector (preferred) OR the fallback below. Either way the
  producer, schema, rate limits, and evidence trail are identical, which is
  what makes this card farmable before the probe resolves.

### Risk analysis for an injectable channel into an autonomous agent

An input channel into an autonomous worker is a control surface and must be
treated like one:

- **Who may inject.** Only Chef (via the dashboard) and the orchestration
  agent identity on chiap08. The nudge CLI records `author` from the invoking
  agent identity on every request; a request without an author is refused.
  Fleet workers themselves may NOT nudge other workers: a worker-writable
  channel into other workers is a lateral-movement primitive and a feedback
  amplifier, and nothing in the current design needs it.
- **Nudge-loop prevention.** The failure mode is an automated supervisor that
  detects "stuck", injects a nudge, changes nothing, detects "stuck" again,
  and loops, producing a new stuck state made of nudges. Controls: (a) hard
  rate limit, at most 3 nudges per card per hour, enforced by the producer;
  (b) an automated nudger must observe at least one NEW session event after a
  nudge before it may send another, so identical-state renudging is
  structurally blocked; (c) automated nudging is off by default, mirroring the
  existing `SKFLEET_WEDGE_MODE=report` posture, and this spec does not arm it.
  Human nudges via dashboard share the rate limit but not the new-event gate.
- **Evidence.** Every injected instruction IS card evidence. It is recorded
  at enqueue time through the coord CLI write boundary, so the card's fold
  shows what was injected, by whom, and when, alongside what the worker then
  did. An instruction that influenced a card's outcome but is absent from the
  card's history would be exactly the false-provenance failure the audit work
  exists to prevent.
- **Content limits.** Nudges are plain text guidance, bounded in size (4 KB),
  delivered as user-role messages. They are not tool invocations and cannot
  grant the worker capabilities it does not already have.
- **Blast radius.** The consumer runs per-worker; a malformed nudge can at
  worst confuse one card's worker, which the existing supervision already
  handles (the card fails review or gets repaired). Compare the wrapper
  hazard below, which is fleet-wide.

## The supervision boundary (this shapes the card decomposition)

`skfleet-worker-wrapper.py` launches every fleet worker on every host. **A
fleet worker editing it is self-surgery**: a bad change kills every worker on
every host simultaneously. Not hypothetical: a deploy that skipped
`pip install -e .` took all five hosts down for an hour on the morning of
2026-09-19.

Therefore:

- **Farmable to fleet workers (all additive, none can take the fleet down):**
  the session stream, the projection format, the aggregator, the dashboard
  view, the liveness module, the digest fix, failed-unit and orphaned-tmux
  cleanup, the guidance-request queue and its evidence recording, tests,
  docs. Every card authored under this spec falls on this side, and none may
  modify `skfleet-worker-wrapper.py`.
- **Supervised, NOT farmed:** the wrapper's `stdin`/rpc change and the
  injection consumer it hosts. Staged on ONE host, verified against live
  workers there, then rolled via `deployment_manifest.PER_HOST_ARTIFACTS`
  (landed 2026-09-19 in #811/#812), which declares per-host artifacts once
  and drives rollout, rollback, and drift detection from that one list. This
  work is done by a supervised session with Chef awake, never by a fleet
  card.

Standing constraints honored by this spec: no changes to the live worker
launch path in any farmed card; no gateway restarts; chiap08 pid 400887
untouched; `SKFLEET_WEDGE_MODE` stays at `report`; noroc2027 and
192.168.0.41 untouched.

## If injection proves impossible

If the probe shows rpc mode only accepts input at start of run:

**Dies:**
- The wrapper stdin/rpc pipe change (the supervised change is simply never
  made in that form).
- The mid-run injection consumer.
- "Add a prompt to a running session" in its literal form.

**Survives unchanged (everything farmed):**
- The entire read-only half: stream, projection, aggregator, dashboard view,
  liveness signal, digest fix, failed-unit cleanup. None of it touches rpc.
- The guidance-request queue, producer CLI, rate limits, and evidence trail.

**The capability degrades instead of dying:** the fallback consumer is
supervised **restart-with-guidance**: for a genuinely stuck worker, stop the
unit cleanly, and relaunch the card's worker with the queued guidance
appended to its initial prompt (start-of-run input is verified to work).
Coarser than mid-run injection, loses in-flight context, but it delivers the
thing Chef actually wants: a way to unstick a worker with a human sentence,
with the same audit trail. The fallback consumer is also wrapper-adjacent
and stays on the supervised side.

## Open questions

1. Does `pi --mode rpc` accept an instruction mid-run? (Opus probing; every
   dependent decision above is marked.)
2. The exact rpc command vocabulary for delivering a user message (same
   probe).
3. Which dashboard surface hosts the view (skdashboard per the 2026-07-16
   spec, or a simpler terminal/web tail first). The card leaves this to the
   implementer as long as it consumes only projected rows.
