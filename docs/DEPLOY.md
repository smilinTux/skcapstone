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

## Drift

`skcapstone doctor` reports three `deploy:*` checks, and the
`sk-deploy-drift.timer` (daily) runs `skcapstone deploy drift --alert`:

- `deploy:editable`: an editable dist in `~/.skenv`;
- `deploy:exec-in-tree`: a unit `ExecStart`/`WorkingDirectory`, Claude Code or
  Hermes MCP command naming a file inside a git working tree;
- `deploy:manifest`: an installed SK version differs from the manifest.

The timer sends one `sk-alert` into the `deploy/drift/<host>` thread when the
findings change and a resolving message when they clear. An unchanged finding
set sends nothing. Accepted exceptions go under `allow_exec_in_tree` in the
manifest, by PR.

## Dev work

Never `pip install -e` into `~/.skenv`. Use `~/.skenv-dev`
(`bash scripts/dev-install.sh` creates it) or a per-worktree venv, and run
tests with the worktree's source on `PYTHONPATH`.
