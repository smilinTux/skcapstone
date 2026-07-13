# Autopilot coord scoring extension

SKOS Autopilot writes grades, edits, and obsolete-closes onto coordination
task files. This is a deliberate, bounded exception to the "tasks are
immutable" protocol. It is documented here and enforced by the single-writer
constraint below.

## Why this is safe

`create_task` is the only historical task-file writer and it is write-once;
claim/complete write only to `agents/*.json`. Because nothing ever re-dumps a
task file through the `Task` model, extra keys written onto a task file are
never stripped. Autopilot adds exactly three mutating writers, and all three
go through one helper:

- `Board._write_task_raw(task_id, mutate)` locates `tasks/<id>-*.json`, loads
  the raw dict (not the model, so non-model keys survive), applies `mutate` in
  place, and writes back atomically (`tmp` + `os.replace`).

## Hard constraint: single-node only

`_write_task_raw` is safe only because `autopilot-daily` is pinned to a single
node (`nodes: [noroc2027]`). A second concurrent task-file writer, or unpinning
the node, reintroduces the Syncthing task-file write-conflict class the
coordination design eliminates. Any future multi-node harness MUST serialize
task-file writes (a coord-side lock) before this can run off a single box.

## The three mutators

- `score_task(task_id, round, score, notes="", harness="", phase=None, ref=None)`
  appends to `meta.autopilot.scores[]`. Idempotent: a re-grade of the same
  `(round, harness)` replaces that entry in place. `ref` routes to `pr` when it
  starts with `http`, else `artifact`.
- `update_task(task_id, description=None, acceptance_criteria=None, add_tags=None,
  run_id=None)` rewrites fields and snapshots each change into
  `meta.autopilot.edits[]` as `{field, old, new, ts, run_id}` so every
  autonomous edit is reversible and auditable. `add_tags` is deduped; a no-op
  merge records no edit.
- `close_task_obsolete(task_id, reason, run_id=None)` records
  `meta.autopilot.obsolete = {reason, run_id, ts}` and appends a human-readable
  line to `notes[]`. Task files carry no status field (done is derived from
  agent files), and autopilot is not the completing agent, so it does not fake
  a done state via an agent file.

## Read helpers (no mutation)

- `unblocked_task_ids()` returns task ids whose dependencies are a subset of the
  union of all agents' `completed_tasks` (Phase-0 unblocked compute).
- `release_stale_claims(agent, older_than_seconds)` releases an agent's
  uncompleted claims when its `last_seen` is older than the cutoff (AgentFile
  has no per-claim timestamp, so `last_seen` is the staleness signal), clears
  `current_task` if it pointed at a released id, and returns the released ids.

## Stored shape

`task.meta.autopilot = {phase, pr|artifact, merge:{sha,pr,branch,ts}, harness,
scores:[{round,score,notes,ts,harness}], edits:[{field,old,new,ts,run_id}],
obsolete:{reason,run_id,ts}}`.

## Surfaces

- CLI: `skcapstone coord score <id> --round N --score S [--notes --harness
  --phase --ref]`.
- MCP: `coord_score` tool (required `task_id`, `round`, `score`).
