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
in `~/.config/sk-deploy/env`, see below). It pins
every package to a released version, lists the third-party requirements the
venv needs, maps each user unit to the packages whose change means it
restarts, and carries smoke commands and per-node profiles. See the docstring
of `skcapstone.deploy.manifest` for the format. The tool reads it at
`origin/main` only, so only a merged manifest can deploy, and the repository
history is the deploy history.

### One manifest repo per cluster

Like the per-cluster config repos, each cluster has its own private manifest
repo, and a node names only its own cluster's: `smilinTux/sk-release` holds the
nor nodes and `smilinTux/sk-release-chi` holds the chi nodes. Both use this
schema. A node never sees another cluster's node profiles, locks or config
pins, and each manifest only lists the packages, tools and services its own
nodes run. A node moving between clusters moves by a PR to each repo.

`~/.config/sk-deploy/env` (also the drift timer's `EnvironmentFile`) names it
with one of:

```bash
SK_DEPLOY_MANIFEST_REPO=smilinTux/sk-release     # https://github.com/<owner/name>
SK_DEPLOY_MANIFEST_URL=git@github-sk-release-chi:smilinTux/sk-release-chi.git
```

`SK_DEPLOY_MANIFEST_URL` takes any URL git can clone, and wins over
`SK_DEPLOY_MANIFEST_REPO`. A variable in the environment wins over the file.
If the configured repo changes, the existing clone in the state dir is
re-pointed on the next fetch.

A node with no GitHub login reads its repos through one read-only deploy key
per repo (GitHub deploy keys are per repo) and an ssh `Host` alias per key, so
no personal login or token is involved. chiap01's setup:

```
# ~/.ssh/config
Host github-sk-release-chi
    HostName github.com
    User git
    IdentityFile ~/.ssh/sk-deploy-chi-release
    IdentitiesOnly yes
Host github-skgateway-config-chi
    HostName github.com
    User git
    IdentityFile ~/.ssh/sk-deploy-chi-gwconfig
    IdentitiesOnly yes
```

The manifest then names that node's config repo by `url:` on the second alias,
for example
`configs: {skgateway: {url: "git@github-skgateway-config-chi:smilinTux/skgateway-config-chi.git", ref: ..., instance: chiap01}}`.
Public repos (the skgateway framework) need no key.

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
5. `try-restart` only the units whose packages changed, and wait (up to 300 s)
   until each is `active` under a NEW `InvocationID`: a restart whose stop is
   still running, or that never really took the unit down, fails the deploy;
6. smoke; on failure switch back and restart the same units. A unit smoke runs
   with `SK_DEPLOY_UNIT`, `SK_DEPLOY_INVOCATION_ID` and `SK_DEPLOY_SINCE` (the
   new start's ActiveEnterTimestamp), so it judges only that start, for example
   `journalctl --user --since "$SK_DEPLOY_SINCE"`, never the previous one's logs.

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
   `<unit>.d/zzzz-sk-deploy-tools.conf` (named to sort after every hand-written drop-in, `zz-*` included), re-stating its `ExecStart*`, `ExecStop*`, `ExecReload`, `ExecCondition`,
   `WorkingDirectory` and `Environment` lines with `current` in place of the
   checkout. Unit files are never edited. Deleting the drop-in reverts a unit;
   removing the tool from the manifest removes its drop-ins on the next apply;
4. restarts the running units whose command changed, and on failure restores
   the previous `current` links and drop-ins.

A tool that provisions itself (a generated launcher, a dependency environment
keyed to its own location) declares `setup`: an argv run once inside the
export at its final path `<name>/<sha12>`, after extraction and before the
export is marked complete or published. A failed setup removes the export, so
nothing is published and the next apply starts clean. Hermes is the example:

```yaml
tools:
  hermes-agent:
    repo: chefboyrdave21/hermes-agent   # the fork, patches carried on sk/<version>
    ref: v0.23.1-sk.1
    branch: sk/v0.23.1                  # the fork's default branch
    checkout: ~/clawd/tools/hermes-agent
    setup: ["/bin/sh", "setup-hermes.sh", "--runtime-only"]
    setup_env: {HERMES_HOME: ~/.hermes}
```

`setup_timeout` (seconds, default 1800) bounds it. Each export gets its own
setup output, so a rollback (`current` back to the previous export) needs no
rebuild.

## Per-cluster config repos

A framework that runs with site configuration (skgateway is the example) is
split the way skstacks is: the public framework is a tool with one pinned
release for every node, and each cluster keeps its configuration in its own
private repository, one directory per instance. Each node names its config
repository and a pinned ref next to the framework's tool:

```yaml
tools:
  skgateway:
    repo: smilinTux/skgateway
    ref: skgateway-v1.1.17          # the framework release, the same everywhere
    checkout: ~/skworld-deploy/skgateway
    setup: [npm, ci, --omit=dev]
nodes:
  node-a:
    tools: [skgateway]
    configs:
      skgateway:
        repo: example-org/skgateway-config-a   # private, per cluster
        ref: 3f2a1c9d0b7e                      # a commit (or tag) on its main
        instance: node-a                       # the directory to deploy
```

`deploy apply` and `deploy tools` then also:

1. export `<instance>/` of the config at its ref (refused unless the ref is
   on the config repo's main) to `~/.local/share/sk-tools/<tool>-config/<sha12>`
   and publish it as `<tool>-config/current`, together with the tool;
2. render every unit file in `<instance>/systemd/` (`*.service`, `*.timer`):
   `@TOOL@` becomes `%h/.local/share/sk-tools/<tool>/current` and `@CONFIG@`
   becomes `%h/.local/share/sk-tools/<tool>-config/current/<instance>`. Any other
   `@NAME@` left in the file, or a path under either export that the new
   exports do not have, refuses the apply before anything is published;
3. install each rendered file as the whole user unit (a header names the
   config commit) and move every drop-in of that unit aside to
   `~/.local/state/sk-deploy/displaced-dropins/<time>/`, so the config repo is
   the unit's only source. No `zzzz-sk-deploy-tools.conf` is written for it;
4. restart the units whose file changed or whose tool or config export moved,
   run their manifest smokes, and on failure put back the previous exports,
   unit files and drop-ins.

An instance can also own files outside systemd, listed in
`<instance>/files.yaml` (an instance with only `files.yaml` needs no units):

```yaml
files:
  - path: ~/.hermes/.env          # block: only the marked lines are the deploy's
    mode: block
    source: hermes.env            # relative to the instance
    format: env                   # env (default), yaml or text
  - path: ~/.config/skcall/join.env
    mode: whole                   # the whole file is the source
    source: join.env
    file_mode: 0o600              # the default
  - path: ~/.hermes/plugins/nctalk
    mode: link
    target: "@TOOL:nctalk@/plugin"
```

A `block` file keeps every other line; the deploy owns only the lines between
`# >>> sk-deploy (managed by skcapstone deploy; edits here are overwritten) >>>`
and `# <<< sk-deploy <<<` (appended at the end the first time; `block:` renames
it). A key the block sets that is also set outside it (`KEY=` or `export KEY=`
for `env`, a top-level key for `yaml`) refuses the apply and names the line, as
does a marker found twice or without its pair. A `link` over a real directory
moves the directory to `~/.local/state/sk-deploy/displaced-files/<time>/`. All
files are rendered and checked against the new exports before anything is
published, written atomically after publish, restart the instance's units when
they change, and are put back (text, absence, mode, moved directory) when a
restart or smoke fails. Nothing is substituted in a file: secrets stay in
0600 files placed from skvault.

So an instance runs exactly (framework ref, config ref), both reviewed by PR,
and a config change ships by: PR in the config repo, merge, PR in the release
manifest bumping that node's `configs.<tool>.ref`, `deploy tools`.

Credentials never go in a config repo. The unit references them as 0600 files
on the node (`EnvironmentFile=-%h/.config/<tool>/secrets.env`, a credentials
path inside the config), kept in skvault, and the config repo's `.gitignore`
names them. A node needs read access to the private config repository: use a
read-only deploy key or a forge URL in `url:`. Drift reports a config export
that is missing or not at the manifest's ref.

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
