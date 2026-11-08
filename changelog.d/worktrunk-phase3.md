Worktrunk becomes the default worktree workflow. `skfleet-wt-gc.timer` (daily, nice 19,
idle IO; required on control and builder-standby) runs the packaged `sk-wt-gc`, which
removes squash-merged, clean, unlocked, unused worktrees and prunes dead entries (locking
those under `/mnt` instead). `sk-wt-task` handles one task across several repos.
`skfleet binary-setup wt` converges a node already at the pin: the sk-managed config, the
`~/skworld-worktrees` root (a symlink to `SKFLEET_WORKTREE_ROOT` on nodes with a fast work
volume, e.g. chiap08 NVMe) and the worktrunk Claude Code plugin, vendored at v0.81.0 so
hooks that run in every session change only with the pinned binary. skcapstone ships a
`.config/wt.toml` whose `wt test` alias pins `PYTHONPATH` to the worktree. See
`docs/fleet/WORKTRUNK.md`.
