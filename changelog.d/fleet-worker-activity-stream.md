- `scripts/fleet/skfleet-worker-stream.py`: a read-only live view into what
  fleet workers are actually doing, projected from `pi`'s own session jsonl
  under `~/.pi/agent/sessions/`. This needs no change to the worker launch
  path: the worker's stdout log is a plain file the wrapper hands to
  `subprocess.Popen`, so it stays 0 bytes until exit (measured on chiap01-04,
  2026-09-19, every live worker's log empty) and cannot be tailed. Session
  files, by contrast, are appended continuously, and every live worker on all
  five chi hosts had written to its own within the last minute.

  The output is a PROJECTION (`{ts,host,card,role,tool,preview}`, ~300 bytes a
  row), never the raw events: sessions run 45-57 MB with a ~14 KB average
  event, so a dashboard cannot consume them verbatim. `--follow` starts at the
  tail rather than replaying that history.

  Three signals that look like they should carry worker progress and do not,
  each now documented in the module so they are not tried again: systemd unit
  state (`active` for a worker doing nothing), CPU (0% both when blocked on a
  lost gateway response and when merely between tool calls), and workspace file
  mtime. The last one is the trap: it reported five of six long-running workers
  silent for 111 to 276 minutes when all five were working, because fleet
  workers do most of their work through tool calls that READ.

  Two liveness defects are covered by regression tests. A card's session glob
  matches sessions from PREVIOUS runs, which made a just-started worker read as
  silent for 8.4 days, so any session file predating its unit by more than
  `RUN_SKEW_S` is refused. And `systemctl list-units` without a state filter
  includes FAILED units, which made `skfleet-worker-glm-l-25ab78c6-repair`
  (failed on chiap02 since 2026-09-10, MainPID=0) read as a live silent worker;
  enumeration is now `--state=active`, with failed units reported separately as
  the cruft they are.
