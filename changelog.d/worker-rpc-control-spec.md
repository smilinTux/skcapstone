- Spec `docs/superpowers/specs/2026-09-19-worker-rpc-control.md`: live worker
  stream to the dashboard plus guided injection via `pi --mode rpc` (not tmux),
  split into a farmable read-only half and a supervised wrapper change, with
  the workspace-mtime liveness trap recorded so nobody rebuilds it.
