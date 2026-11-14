# Deploying: production runs released main

`~/.skenv` is what every unit, timer, cron job and MCP server on a node runs.
It must hold released builds of main, installed non-editable. An editable
install there makes production whatever branch a shared checkout happens to
be on, including another session's uncommitted edits. A change reaches
production only as:

PR, merge to main, release (a tag on main, published), manifest PR, rollout,
restart, verify.

## The release manifest

One YAML file, `manifest.yaml`, in a repository of its own (named on each node
in `~/.config/sk-deploy/env` as `SK_DEPLOY_MANIFEST_REPO=owner/name`). It pins
every package to a released version, lists the third-party requirements the
venv needs, maps each user unit to the packages whose change means it
restarts, and carries smoke commands and per-node profiles. See the docstring
of `skcapstone.deploy.manifest` for the format. The tool reads it at
`origin/main` only, so only a merged manifest can deploy, and the repository
history is the deploy history.

Package sources:

- `pypi`: `name==version` from PyPI. The release tag (`v{version}` unless
  `tag:` says otherwise) must be on the repository's default branch.
- `git`: a private package installed straight from a tag on its default
  branch, non-editable. No wheel host is needed.

## Commands

```bash
skcapstone deploy plan                 # what would change and restart
skcapstone deploy apply                # roll this node onto the manifest
skcapstone deploy apply --no-restart   # build, verify and switch only
skcapstone deploy rollback             # back to the previous release
skcapstone deploy status               # what this node runs
skcapstone deploy drift [--alert]      # editable installs, exec in trees, versions
```

`apply`, in order, each step able to stop before production changes:

1. pre-flight: every release tag is on its default branch (GitHub compare API);
2. build `~/.skenv-releases/<time>-<manifest>` with uv, non-editable;
3. verify that venv: imports, every console-script target loads, `pip check`,
   nothing editable, and no `bin/` entry of the live venv is lost (name
   deliberate drops with `--allow-missing-bin`);
4. switch `~/.skenv` to it with one atomic rename (the first run moves the old
   directory venv to `~/.skenv-releases/legacy-<time>`, the rollback target);
5. `try-restart` only the units whose packages changed;
6. smoke; on failure switch back and restart the same units.

State is host-local in `~/.local/state/sk-deploy/`, never under the
Syncthing-shared `~/.skcapstone`. Running Claude Code sessions keep their MCP
processes until they restart; new sessions get the new release.

## Tools: scripts units run

Many units run a script or a small server that is not a package entry point
(`scripts/nostr_relay.py`, a `WorkingDirectory` holding config). Running those
from a checkout makes production whatever branch that checkout is on, and a
unit breaks the moment the checkout loses the file. The manifest's `tools:`
section releases them instead:

```yaml
tools:
  skchat:
    package: skchat-sovereign     # repo, branch and default ref from the package
    ref: 4f588c8                  # optional: tag or commit, must be on main
    checkout: ~/clawd/skcapstone-repos/skchat   # the tree units used to run
    paths: [scripts]              # optional: export and rewrite only these
  lumina:
    url: https://forge.example/owner/lumina.git # any git URL (private forge)
    ref: 0a1b2c3
    checkout: ~/clawd
    paths: [tools/anthropic-proxy, scripts/battery-cycle-watch.sh]
```

`skcapstone deploy apply` (and `skcapstone deploy tools`, which skips the venv
rebuild) then, for each tool:

1. fetches a bare mirror into `~/.local/state/sk-deploy/tool-src/`, resolves the
   ref and refuses it unless it is on the default branch;
2. writes `git archive` of it to `~/.local/share/sk-tools/<name>/<sha12>` and
   switches `<name>/current` to it atomically;
3. gives every user unit that names a path under `checkout` one managed drop-in,
   `<unit>.d/zzzz-sk-deploy-tools.conf` (named to sort after every hand-written drop-in, `zz-*` included), re-stating its `ExecStart*`,
   `WorkingDirectory` and `Environment` lines with `current` in place of the
   checkout. Unit files are never edited. Deleting the drop-in reverts a unit;
   removing the tool from the manifest removes its drop-ins on the next apply;
4. restarts the running units whose command changed, and on failure restores
   the previous `current` links and drop-ins.

## Drift

`skcapstone doctor` reports the `deploy:*` checks, and the
`sk-deploy-drift.timer` (daily) runs `skcapstone deploy drift --alert`:

- `deploy:editable`: an editable dist in `~/.skenv`;
- `deploy:exec-in-tree`: a live unit's `ExecStart`/`WorkingDirectory`, or a
  Claude Code or Hermes MCP command, naming a file inside a git working tree;
- `deploy:exec-missing`: a live unit's program, interpreter script or working
  directory does not exist (it fails on its next start);
- `deploy:manifest`: an installed SK version differs from the manifest;
- `deploy:tools`: a tool export is missing or not at the manifest's ref.

A unit counts as live when it is enabled or active, when its timer, path or
socket is, or (for a template) when any instance is. Units nothing can start
are listed as dormant by `deploy drift --json` and never alert: disable a dead
unit and it stops being a finding.

The timer sends one `sk-alert` into the `deploy/drift/<host>` thread when the
findings change and a resolving message when they clear. An unchanged finding
set sends nothing. Accepted exceptions go under `allow_exec_in_tree` in the
manifest, by PR.

## Dev work

Never `pip install -e` into `~/.skenv`. Use `~/.skenv-dev`
(`bash scripts/dev-install.sh` creates it) or a per-worktree venv, and run
tests with the worktree's source on `PYTHONPATH`.
