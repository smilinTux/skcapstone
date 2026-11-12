# Vendored: worktrunk Claude Code plugin v0.81.0

Source: https://github.com/max-sixty/worktrunk/tree/v0.81.0/plugins/worktrunk
(license MIT OR Apache-2.0). Copied verbatim except: the icon is dropped, plugin.json
gains `version`, and the marketplace is renamed `sk-worktrunk` so it never collides
with the upstream marketplace.

Why vendored: a marketplace added from GitHub tracks upstream `main`, so its hooks
(which run in every Claude Code session) would change without review. This copy only
changes when a reviewed commit bumps it, in lockstep with the pinned `wt` binary in
`skcapstone/fleet/binaries.py`. To bump: re-copy `plugins/worktrunk` from the new tag
and update the version in both places.

What it does: routes Claude Code `isolation: "worktree"` subagents through
`wt switch --create` / `wt remove` (so they land under ~/skworld-worktrees with project
hooks), auto-approves entering a worktree at wt's own path, and keeps the 🤖/💬
activity markers in `wt list`.
