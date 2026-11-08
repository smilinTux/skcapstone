# Worktrunk (`wt`): how agents make and clean up worktrees

Worktrunk (https://github.com/max-sixty/worktrunk) is the fleet's worktree manager. It is a
pinned skfleet binary (`skcapstone/fleet/binaries.py`, version and sha256 per arch in code),
required on the control, builder-standby and worker-gpu profiles.

Why it, in one line: its merge detection understands **squash merges** (tree match,
simulated merge, patch-id), so it can prove a worktree's work landed even though the
branch's commits never reach `main`. Ancestor checks cannot, which is why hundreds of
merged worktrees piled up (2026-11-07: 23 on noroc2027, about 130 plus 838 dead entries
on chiap08).

## Rules for agents

1. **Create** a worktree with `wt switch -c <branch> --no-cd` from the repo's main
   checkout. Never `git checkout -b` in a shared checkout.
2. **Work** in the printed path (`~/skworld-worktrees/<repo>.<branch>`). Run tests with
   `wt test ...` where the repo defines it (it pins `PYTHONPATH` to the worktree).
3. **Merge** through a PR. `wt merge` is refused by the fleet config, because it
   fast-forwards a *local* `main` and would skip review and CI.
4. **Remove** with `wt remove <branch>` once the PR is merged, or leave it: the daily
   `skfleet-wt-gc.timer` removes it once worktrunk proves it integrated. `wt remove`
   refuses uncommitted changes (`-f` backs them up to `refs/wt-backup/<branch>` first) and
   unmerged branches (`-D`). Never `rm -rf` a worktree.
5. **Several repos, one task:** `sk-wt-task add <task> skcapstone skcoord skmemory` makes a
   worktree per repo and a task folder `~/skworld-worktrees/_tasks/<task>/` linking them.
   Run the agent from the task folder. `sk-wt-task rm <task>` cleans up.
6. **Claude Code subagents** with `isolation: "worktree"` go through `wt` too (the worktrunk
   plugin's `WorktreeCreate` / `WorktreeRemove` hooks), so they land in the same root and
   get the same GC.

## What skfleet manages

| Piece | Where | How it converges |
|---|---|---|
| `wt`, `git-wt` 0.81.0 | `~/.local/bin` | `skfleet install --apply --only wt` (download, verify the in-code sha256, atomic replace) |
| User config | `~/.config/worktrunk/config.toml` | written while it carries `# sk-managed`; delete that line to own the file |
| Worktree root | `~/skworld-worktrees` | plain dir, or a symlink to `SKFLEET_WORKTREE_ROOT` when set |
| Claude Code plugin | marketplace `sk-worktrunk` | vendored at v0.81.0 in `data/worktrunk-claude-plugin/` (see its `VENDORED.md`) |
| Daily GC | `skfleet-wt-gc.timer` | required unit on control and builder-standby |

`install --apply` only reaches a binary that is missing or off the pin. On a node already
at the pin, converge the config, root and plugin with:

```bash
skfleet binary-setup wt
```

A drift report (`skfleet node doctor`, `skfleet install --check`) shows
`missing_required_binaries: wt` when it is absent **or at another version**: bumping the pin
in `binaries.py` is the upgrade path.

## Per-node storage (fast volumes)

The config always says `~/skworld-worktrees`. Where that physically lives is per node:

```bash
# chiap08: worktrees on the NVMe work volume, not the 85%-full root disk
echo 'SKFLEET_WORKTREE_ROOT=/srv/work/worktrees' >> ~/.config/environment.d/10-skfleet.conf
SKFLEET_WORKTREE_ROOT=/srv/work/worktrees skfleet binary-setup wt
```

`binary-setup` turns an **empty** `~/skworld-worktrees` into the symlink. It never moves or
relinks a populated directory; it reports it instead.

## GC (`sk-wt-gc`)

```bash
sk-wt-gc --discover ~/clawd/skcapstone-repos ~/skworld-worktrees      # dry run
sk-wt-gc --apply --no-fetch --discover ~ /srv/work                    # what chiap08 runs
```

It removes only worktrees that are integrated, clean, unlocked (live Claude subagents lock
theirs) and not the cwd of any process. It prunes entries whose directory is gone, except
under `/mnt` (an offline mount reads as gone): those are locked with a reason instead.
`--no-fetch` is the safe direction on loaded hosts (a stale `main` only means fewer
removals). `--approve` pre-approves a repo's `.config/wt.toml` hooks, only from a clean
main checkout on its default branch, so hook commands change only through review.

Per-node roots for the timer go in `~/.config/skfleet/wt-gc.env`:

```bash
WT_GC_ROOTS="/home/skuser01 /srv/work"
```

## Project hooks (`.config/wt.toml`)

Each repo may ship hooks for fresh per-worktree dependencies (never a symlinked
`node_modules` or a shared editable install) and `{{ branch | hash_port }}` ports so a
worktree never collides with prod on a default port. `pre-*` hooks block and abort on
failure; `post-*` run in the background.

## Out of scope (deliberately)

- skfleet's own worker workspaces (`~/.skcapstone/fleet/workspaces`) are clones, not
  worktrees, and their deletion stays `# intentionally-unwired` (custody evidence).
- Worker briefs that run inside a bwrap sandbox keep making worktrees *inside*
  `SKFLEET_WORKSPACE`; `~/skworld-worktrees` is outside the sandbox mount.
