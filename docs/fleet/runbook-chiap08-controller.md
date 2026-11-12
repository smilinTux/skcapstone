# Runbook: orchestrating the fleet FROM chiap08

`chiap08` is registered `role=control` (`~/.skcapstone/fleet/objects/node/node-chiap08.json`)
and is the manual controller box for the **chi** estate. This runbook is the
documented entrypoint for that role. It records the one rule that decides
whether a controller run means anything, and the entrypoints that already exist.

Ported from #810, trimmed to what is verifiable on current main: the original
also carried a 2026-09-19 snapshot (installed versions, per-node reach table,
`pi` catalog state, gap list). Those were point-in-time facts and are not
repeated; check them live with `skfleet nodes` and `pi auth check`.

## The estate boundary rule: read this first

**A controller must run inside the estate it controls.** `nor` (`noroc2027`)
and `chi` (`chiap01-04`, `chiap08`, ...) are disjoint Syncthing shares with
separate CardStores (see `starting-a-new-project.md`, "One estate, one board").
Fleet readiness gates read a **local file**,
`~/.skcapstone/fleet/status/node-<host>/readiness/verdict.json`
(`src/skcapstone/fleet/staged_rollout.py::_readiness_verdict`), which only
reaches the estate the node belongs to.

Run `skfleet rollout` from `noroc2027` against chi hosts and every chi verdict
file is simply absent there. `_readiness_verdict` returns `None` with
`no readiness verdict published for <node>`, so the rollout halts on the first
node exactly as if it had failed a real health check. That is a fault in where
the controller was run, not in the node.

- Estate-local paths: `~/.skcapstone/fleet/status/**`,
  `~/.skcapstone/fleet/objects/**`, `~/.skcapstone/coordination/**`.
- A "no readiness verdict published" reason for a node you know is up is the
  signature of this mistake. Check which estate you are on before treating it
  as a node problem.
- To orchestrate chi, SSH to a chi host (`chiap08` is the standing one) and run
  the command there. Do not run chi fleet commands from `noroc2027`, and do not
  run nor fleet commands from a chi host.

## Entrypoints (nothing new needs building)

`skfleet rollout` wraps `staged_rollout.execute_rollout` and is dry-run by
default:

```bash
ssh chiap08
cd ~/work/skcapstone   # or pass --repo-root explicitly
skfleet rollout --node chiap01 --node chiap02 --node chiap03 --node chiap04 --json
```

Without `--apply` there is no network call and no filesystem write for any
node. With `--apply` it visits nodes in the order given and halts at the first
node that fails to deploy or fails its gate; later nodes are untouched. See
`skfleet rollout --help` for `--repo-root`, `--remote-repo-root`, `--strict`
and `--json`.

Other entrypoints that run from chiap08 as they are:

- `skfleet nodes` and `skfleet describe` for the board and per-node objects.
- `skfleet node drift --json` for this node's own drift. Remote nodes are
  graded over SSH by the rollout gate, not by running this against them.
- `skfleet rollback --node ... [--apply]`, the same staged halt-on-first-failure
  mechanism in reverse, using each node's recorded previous manifest.
- `skcapstone coord ...` for the board. There is no separate `skcoord` binary.

Do not build a parallel script that re-implements any of the above.
