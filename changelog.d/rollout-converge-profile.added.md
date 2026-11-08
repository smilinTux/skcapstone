A rollout now applies each node's install profile after it deploys. The new last deploy
step, `converge_profile` (forward and rollback, guarded so an older revision skips it),
runs the profile installer with enable and start, then the config and post-install steps
of every required binary already at its pin. It is freeze- and opt-in-gated and never
fails the deploy itself. Before this, worktrunk's `wt` setup and `skfleet-wt-gc.timer` had
to be done by hand on every builder after the code rolled (2026-11-08).

`skfleet-auto-rollout` adds ziowk01 to the default ring, last. A host can now list lock
paths in `~/.config/skfleet/rollout-busy-locks`. While one is held, the ring skips that
host for the tick and retries it on the next, so a release gate running on ziowk01 never
gets a pip install in the middle of its run.
