# Fleet goldfish launch isolation

Fleet coding workers set `SKMEMORY_HOOKS=off` in the environment of the actual
child process. The switch is present at both kinds of isolation boundary used by
the current launcher:

- `skfleet-rotate.py` puts it on the transient systemd service environment, so
  the worker wrapper and its Pi child inherit it even when the manager supplies
  the ambient environment.
- Builder and review Pi commands restore it explicitly after `/usr/bin/env -i`.
- Native Codex and Cursor commands restore it explicitly after bwrap
  `--clearenv`.

This is launch isolation only. It does not install or remove hooks, change an
SKMemory installation or configuration, inject a persistent identity, or save
anything at session end.

## Source launch inventory and gaps

| Surface | Actual child | Isolation seam |
| --- | --- | --- |
| Rotation dispatcher and lifecycle-seat/review lanes | Pi through `skfleet-worker-wrapper.py` | transient systemd service environment |
| Destination builder | Pi | `/usr/bin/env -i` command built by `builder_dispatch.worker_command` |
| Destination review | Pi through `skfleet-worker-wrapper.py` | `/usr/bin/env -i` child built by `production_builder.launch_review` |
| Native Codex lane | `codex exec` | bwrap `--clearenv` in `native_codex.sandbox_command` |
| Cursor plan lane | `cursor-agent` | bwrap `--clearenv` in `production_cursor_sandbox.sandbox_command` |

There is no current fleet child launcher for Claude or OpenCode in this
worktree. Current source also observes existing herdr agents but does not create
the herdr daemon or its panes. Those external/legacy launchers must set the same
variable themselves; this switch does not fence legacy hook scripts that ignore
`SKMEMORY_HOOKS` or processes started outside these seams.
