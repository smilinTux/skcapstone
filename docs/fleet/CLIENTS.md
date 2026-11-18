# skfleet-clients: one version, one path, one config for pi, opencode, codex, claude

Status: design, 2026-11-18. Owner: chiap08 authority host. Sibling of
skfleet-auto-rollout (see DEPLOY-FROM-MAIN.md); it reuses that job's ring order,
reachability probe, busy locks, HALT streak escalation and ROLLED/HALTED skmail.

## Problem

Measured 2026-11-18: four clients on eight chi hosts sat at mixed versions
(opencode 1.19.6 vs 2.2.5), in four install paths (npm-global, ~/.local/bin,
~/.opencode/bin, /usr/local/bin), and every host had a different pi models.json.
A new gateway model (gpt-6.2-sol/astra) meant hand-patching each host.

## Split

- Data, `skgateway-config-chi/clients/`:
  - `pins.yaml`: client versions, install source, ring order.
  - `templates/`: the managed fragment of each client config.
  - `hosts/<host>.yaml`: per-host overrides (skip a client, extra provider).
- Code, `skcapstone/src/skcapstone/fleet/clients/`, CLI
  `skcapstone fleet clients {render,plan,apply,drift,smoke,upstream,rollback}`.
- One job on chiap08: `skfleet-clients.timer` (every 5 min) plus
  `skfleet-clients-upstream.timer` (daily). They share one lock with each other
  (not with skfleet-auto-rollout: the two touch different files).

## pins.yaml

```yaml
schema: 1
install_root: ~/.local/opt/sk-clients   # <root>/<client>/<version>/
bin_dir: ~/.local/bin                   # stable symlinks, first on PATH
ring: [chiap08, chiap03, chiap01, chiap02, chiap04, chiap10, ziowk01, chiwk12]
clients:
  pi:       {version: 1.5.2,   source: npm, package: "@earendil-works/pi-coding-agent"}
  codex:    {version: 0.170.0, source: npm, package: "@openai/codex",
             gateway_env: SKGATEWAY_CODEX_CLI_VERSION}
  claude:   {version: 2.1.327, source: npm, package: "@anthropic-ai/claude-code"}
  opencode: {version: 2.2.5,   source: release, url: ..., sha256: ...}  # npm stops at 1.19.x
```

## Install path

Each version installs into its own directory (`npm install --prefix
<root>/<client>/<version>` or an unpacked, sha256-checked release tarball). The
host's `~/.local/bin/<client>` symlink is flipped atomically (`ln -sfn tmp; mv`).
A running session keeps its old inode, so a flip never needs the host idle. The
previous version directory is kept for rollback; older ones are pruned.

After a host passes, shadowing installs are removed (`npm -g uninstall`, stale
`~/.opencode/bin`). Root-owned ones (chiwk12 `/usr/local/bin/codex`) are not
touched; drift reports them. T3 `providerInstances.binaryPath` is pointed at the
`~/.local/bin` symlink once, so later version flips need no T3 change.

Self-updaters off, via managed keys: opencode `autoupdate: false`, codex
`check_for_update_on_startup = false`, claude `env.DISABLE_AUTOUPDATER = "1"`,
pi's update check likewise.

## Configs: managed keys, not whole files

Live configs hold per-host state (codex trusted projects, T3 settings, MCP
lists), so a template owns only named keys and the renderer merges them into the
live file, leaving the rest as is:

- pi `models.json`: the gateway provider blocks (`skgateway`, `skgw-*`). Their
  `models` lists are generated from `GET chiap01:18790/v1/models` by per-provider
  rules in the template (owner, id pattern, advertised). `baseUrl`, `compat` and
  the `x-app`/`x-session-id` headers come from the template. Non-gateway
  providers and the `skfleet_catalog_sync` key are left alone.
- opencode `opencode.json`: `provider.skgateway`, `autoupdate`.
- codex `config.toml`: the update-check key (codex models come from its own login).
- claude `settings.json`: `env.DISABLE_AUTOUPDATER`.

Secrets never leave a host: the merge runs on the target (a stdlib-only payload
over ssh, so it does not depend on the host's skcapstone version), keeps the live
`apiKey`, and reports diffs with secret values masked. Every write keeps a
timestamped backup under `<config dir>/backups/skfleet-clients/` (last 10).

The pi wrapper's launch-time `sk-pi-gateway-sync.py` rewrites the skgateway
block on every launch and would fight this; it is switched off on managed hosts
(the 5 minute catalog watch replaces it).

## Triggers

1. Catalog change (5 min tick). Fingerprint the gateway list (id, owner, context,
   max output, vision, advertised). If it differs from the last rolled one,
   render and roll configs around the ring. `POST /admin/models/refresh` is not
   called: the job follows the gateway, it does not drive it.
2. Pins change (5 min tick). If `origin/main` of the config repo has a pins sha
   not yet rolled, roll versions around the ring.
3. Upstream check (daily). `npm view` / release feed per client. A newer version
   opens a pin-bump PR. Before it can merge, chiap08 installs the candidate into
   its version directory without flipping the symlink and runs the canary smoke
   against that binary. Pass: the PR gets the smoke result and auto-merge is
   armed. Fail: PR stays open with the failure, skmail sent.
   - A codex bump also bumps `SKGATEWAY_CODEX_CLI_VERSION` in
     `chiap01/systemd/skgateway-chiap01.service` in the same PR (upstream gates
     new models on client_version) and is held for Chef: it restarts the gateway.
   - Major version bumps are held for Chef.

## Rollout and gates

Ring order from pins.yaml, canary chiap08 first. Per host, the same pre-checks as
skfleet-auto-rollout (ssh probe, `rollout-busy-locks`, skip and retry next tick),
then install/flip and merge configs, then the host gate:

- `<client> --version` equals the pin, `command -v <client>` resolves to the
  bin_dir symlink.
- Each managed config parses and contains the rendered keys.
- `pi --list-models` lists the rendered gateway ids.

Canary smoke (chiap08 only, and on every upstream candidate): one tiny real
prompt per client (`pi -p`, `opencode run`, `codex exec`, `claude -p`) through
the configured route, answer must contain a fixed token.

A failed gate restores that host (previous symlink, config backups), stops the
ring there (later hosts keep the old state), exits nonzero (OnFailure alert),
skmails `HALTED at <host>` and counts toward the halt streak. A full ring skmails
`ROLLED`. Per-host state lives in `~/.local/state/skfleet-clients/`.

## Drift and rollback

`skcapstone fleet clients drift` prints the host x client table (version, path,
config hash, shadowing installs) and exits nonzero on drift. The 5 min tick logs
it; persistent drift skmails once a day. `rollback --host H --client C` flips
back to the kept version; a bad pin is reverted by a PR that reverts the bump.

## Open decisions (Chef)

1. Auto-merge in skgateway-config-chi: chiap08's gh token has READ there and the
   repo has auto-merge off. Proposed: enable it and give the chiap08 bot write,
   with the job only arming auto-merge on PRs that touch nothing but
   `clients/pins.yaml`. Until then the job opens the PR with its smoke result and
   Chef merges.
2. Codex and major bumps held for Chef (above).
3. Switching off the pi launch-time gateway sync on managed hosts.
