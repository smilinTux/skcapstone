# Rollout drift: is what we merged actually running (nimble-factory Plan B3, phase 1)

**Spec:** `docs/superpowers/specs/2026-09-16-nimble-factory-design.md`, sections
A.4 and A.6, as corrected in
`docs/superpowers/plans/2026-09-17-rollout-observability.md` (phase 1) and
`docs/superpowers/plans/2026-09-17-staged-rollout.md` (phase 2).
**Code:** `src/skcapstone/fleet/deployment_manifest.py`,
`scripts/fleet/skfleet_readiness.py`, `src/skcapstone/fleet/rollout_drift.py`,
`src/skcapstone/fleet/rollout_history.py`, `src/skcapstone/fleet/staged_rollout.py`,
`skcapstone fleet node drift`, `skcapstone fleet rollout`,
`skcapstone fleet rollback` (`src/skcapstone/fleet/cli.py`).

Sections 1-3 below are phase 1 (build a manifest, gate a node, compare a
node against a manifest). Section 4 is phase 2 (stage a deploy across
several nodes, one at a time, and roll one back).

## Why this exists

`pip show skcapstone` and the module's `__version__` agreed on every host
throughout each of these, measured in one session on the live fleet:

- Three different `skmail` binaries across five hosts, none matching the repo.
  The file was never in `pyproject.toml`'s `script-files`, so it belonged to
  no package a version check could see.
- `~/.local/bin/skfleet-rotate.py`, the dispatcher script, is a deploy step
  separate from the package. It sat stale on hosts whose package was current.
- A seat unit stayed FAILED on chiap08 for weeks.
- `skfleet-rotate.timer` was active but not enabled (no
  `timers.target.wants` symlink) on all three rotate hosts for at least
  seven weeks. A reboot on any of them would have stopped fleet dispatch
  fleet-wide, and nothing was reporting it.

A version string proves what tag was installed. It proves nothing about
whether the installed files match that tag, whether a hand-copied script is
current, or whether a unit that is supposed to be enabled actually is. The
three tools below compare content and live state instead, and are read-only:
nothing here writes to a host, restarts a unit, or installs anything.

## 1. The deployment manifest

`deployment_manifest.build_manifest(repo_root, home) -> dict` pins what a
node is supposed to run, derived from a checkout, never hand-typed:

| Field | Source |
|---|---|
| `revision` | `sha256` of the other four fields, canonicalized. Two manifests built from identical inputs are byte-identical; this is a content digest, not the wall-clock timestamp the spec's example shows, so a caller comparing two manifests for equality (built at different times, from the same repository state) does not see a false difference. Not currently read by `detect_drift` itself (below), which compares each field against live host state directly rather than manifest-to-manifest. |
| `git_sha` | `git rev-parse --short=8 HEAD` against `repo_root`. |
| `package_version` | The running interpreter's installed `skcapstone` distribution version. |
| `required_env` | AST-parsed out of the dispatcher (`scripts/fleet/skfleet_readiness.required_env`), reused rather than duplicated, so this list can never drift from what the readiness gate itself checks. |
| `units` | Every unit file shipped under the canonical `systemd/` tree. |

Example, generated read-only against this repo:

```json
{
  "revision": "6697b10a3f38a4bb82dd4473c23db158fc85d22fc918a86fce9c60e0a015f40f",
  "git_sha": "41b54ee8",
  "package_version": "0.15.168.dev224+ga62c0061.d20260917",
  "required_env": [
    "SKFLEET_GATEWAY_URL",
    "SKFLEET_GLM_TARGET",
    "SKFLEET_TARGET"
  ],
  "units": ["skcapstone.service", "skfleet-atlas.service", "sknoded.service", "..."]
}
```

`write_manifest(path, manifest)` writes it atomically (temp file, single
`os.replace`). `build_manifest` itself stays a pure function that a caller
supplies `repo_root`/`home` to; `skcapstone fleet node drift` (below) is one
such caller, and it never persists what it builds -- both halves of that
command's report-only contract (build, then compare) stay in memory.

`skcapstone fleet node manifest` is `write_manifest`'s real caller: it
builds the same manifest `node drift` builds and publishes it into the
FLEET tree (`fleet/paths.py`'s `default_paths()`, i.e. `$SKFLEET_ROOT` or
its documented default) at `status/node-<node>/manifest/manifest.json`,
the same `status/<node>/<kind>/<name>.json` shape every other status write
in the package uses -- through `default_paths()` deliberately, not a path
built from `--home`, because this is fleet state and `paths.py` is the one
module allowed to name where that tree lives
(`tests/fleet/test_root_relocation.py`). Unlike `node drift`/`node doctor`
it writes on purpose, so it carries no report-only contract. `node drift`
itself still compares against a freshly built manifest, not this pinned
one; publishing a comparison artifact a future check could read against is
what closes the "nothing publishes a manifest" gap without changing what
`node drift` means today.

## 2. The readiness verdict

`scripts/fleet/skfleet_readiness.py` checks, before a rotate host's
dispatcher would run, that every `required_env` variable is actually set and
every seat module actually imports under the target interpreter. It now has
a caller: `skfleet-readiness.service` / `.timer`, installed from
`systemd/skfleet-readiness.service` (mirrored byte-identical into
`src/skcapstone/data/systemd/`), running every 15 minutes
(`OnBootSec=5min`, `OnUnitActiveSec=15min`). That pair is now part of
`skcapstone.systemd.ALL_UNITS`, the one list `install_service` (invoked by
`skcapstone daemon install`, the live install path measured on chiap01)
actually copies and enables on a host -- shipping the unit files under
`systemd/` was not enough by itself, confirmed absent on chiap01 before this
was wired in.

The gate is scope-aware: it checks the dispatcher's environment only on a
host where `skfleet-rotate.service`'s paired timer is genuinely active or
boot-enabled. On a host whose role never runs the dispatcher (chiap08, a
seat-only host), each mandatory variable reports `SKIP` with the reason
named, and that never counts against readiness. An undetermined scope (the
paired timer cannot be found or asked) is never silently treated as "skip":
it reports `FAIL` and fails the gate, on the rule that unknown is never
ready.

The verdict is written atomically to:

```
%h/.skcapstone/fleet/status/node-%H/readiness/verdict.json
```

(`%h`/`%H` are systemd specifiers: the invoking user's home and the local
hostname.) Node-scoped deliberately: `~/.skcapstone` is one Syncthing folder
shared across the whole fleet, so an unscoped path would have every host
overwrite the same file. The path shape (`status/<node>/<kind>/<name>.json`)
matches `src/skcapstone/fleet/paths.py`'s existing convention
(`kind="readiness"`, `name="verdict"`).

```json
{"ready": true, "checked_at": "2026-09-17T21:00:00Z", "lines": ["OK ...", "SKIP ...", "READY"]}
```

Run it by hand against a host with `--env-from-systemd skfleet-rotate.service
--verdict-path <path>`; see `skfleet_readiness.py --help` for the full flag
list (`--rotate-script`, `--units-dir`, `--python-bin` are required).

## 3. The drift check: `skcapstone fleet node drift`

```bash
skcapstone fleet node drift [--repo-root PATH] [--home PATH] [--json] [--strict]
```

Builds a fresh manifest from `--repo-root` (default: `$SKCAPSTONE_REPO_ROOT`,
else `~/work/skcapstone`, the shared-checkout convention live on
chiap01/02/03/04/08) and compares it against what `--home` (default: `$HOME`)
actually has installed: a content digest of each shipped unit file, a
content digest of the installed dispatcher script and every other
`pyproject.toml` `script-files` entry (`~/.skenv/bin/<name>`, the exact
mechanism the `skmail` incident hid behind -- see the `script` kind below),
the installed distribution's embedded git commit (not its semantic version,
which is exactly what let that incident hide), whether each in-scope unit's
enabled state agrees with its active state, and whether any in-scope unit's
own `ActiveState` is `failed`. Local-node-only, like
`fleet node doctor`: grading a remote host from a local checkout would be a
confident wrong answer, not a report. Read-only and cheap enough to run from
a systemd timer, though no timer invokes it yet; today it is operator-run,
on demand.

**Not the same thing as `skcapstone fleet node doctor`.** `doctor` diffs a
node's live inventory against a role PROFILE (what kind of node this is
supposed to be) and works from any terminal with no repo checkout needed.
`drift` compares a node's installed files and state against a git checkout's
CONTENT (what commit this node should be running). A node can pass one and
fail the other; they answer different questions.

### Reading the output

Every drift line carries a `kind`:

| `kind` | Meaning |
|---|---|
| `changed` | The artifact is installed, but its content differs from what the manifest pins. Applies to unit files, the dispatcher script, and every other `pyproject.toml` `script-files` entry (artifact `script:<name>`) -- the `skmail` incident's shape, generalised to any script installed the same way, not just that one by name. |
| `enablement_mismatch` | A unit's `ActiveState` and enabled (`UnitFileState`) disagree, e.g. active but not enabled. This is the timer-enablement incident above: no content digest can catch it, because the unit *file* was correct. |
| `failed` | A unit's own `ActiveState` is literally `failed`. Self-consistent with a disabled `UnitFileState`, so `enablement_mismatch` never fires for it -- this is the chiap08 incident: a seat unit sat FAILED for weeks and no other check ever emitted a finding for it. Unambiguous regardless of role: a unit whose role does not apply here was never installed, so systemd reports it inactive, never failed. |
| `missing` | The artifact was not found at all. |
| `git_sha` (artifact) | The installed distribution's embedded commit differs from (or is entirely absent from) what the manifest expects. |

**`changed`, `enablement_mismatch`, `failed`, any `git_sha` finding, and a
`missing` `script:<name>` finding are shown by name in the default text
output: they are unambiguous regardless of what this host's role is.** A
`missing` unit or dispatcher-script finding is different: this estate has
no per-host role manifest, so "this host's role never installs that unit"
cannot be told apart from "a rollout should have installed it and did
not." Run against this checkout, most `missing` findings are the former: a
workstation or a seat-only host legitimately never carries every unit the
full package ships. A `missing` `script:<name>` finding carries no such
excuse -- pip installs every `pyproject.toml` `script-files` entry
unconditionally on every host with the package installed, so there is no
role for which one is supposed to be absent (measured live:
`skfleet_readiness.py` is genuinely missing from chiap01's
`~/.skenv/bin`). Printing every ambiguous finding by default buried the six
lines that actually mattered under fourteen that did not, which is exactly
the kind of noise that gets a check ignored within a week. So the default
text output lists every unambiguous finding by name and folds only the
role-ambiguous `missing` unit/dispatcher findings into one summary line
instead:

```
$ skcapstone fleet node drift
node-noroc2027	20 drift(s) against manifest git_sha=b421c0d1
  changed              unit:skcapstone.service                  expected='a1ed1a6e...' found='2338a7d8...'
  changed              unit:skcapstone@.service                 expected='454b4b6a...' found='a531456d...'
  changed              unit:skfleet-niobe-live.service          expected='a8c50927...' found='6f4d6efe...'
  changed              unit:skfleet-niobe.service               expected='a426756e...' found='6010966c...'
  changed              unit:sknoded.service                     expected='29da5446...' found='33a3f8ed...'
  changed              git_sha                                  expected='b421c0d1' found='723e6a98'
  14 unit(s)/dispatcher script reported missing, not listed: this estate has
  no per-host role manifest, so 'this host's role never installs it' cannot
  be told apart from 'a rollout should have installed it and did not'.
  Re-run with --json to see each by name.
```

`--json` and `--strict` both still act on **every** finding, `missing`
included, but only `--json` changes what is *printed*: it emits the full
list unconditionally, so that is the flag the summary line points at.
`--strict` changes only the exit code (1 if any finding at all is present,
missing included); its text output is identical to the default, unaffected
by the flag, so telling an operator to pass `--strict` to see names by hand
would be pointing them at a flag that does not do that.

A clean host reports:

```
$ skcapstone fleet node drift
node-chiap01: no drift (matches b421c0d1)
```

## 4. Staged rollout and rollback (nimble-factory Plan B3, phase 2)

**This is a human-invoked mechanism, not an autonomous actuator.** Nothing
schedules it, nothing calls it from a drift finding, and nothing about it
changes ATLAS's authority (see below). It exists so that the four-step
manual procedure `docs/fleet/activation-runbook.md`'s rollout ordering
constraints already document (package, then script, then converge, then
verify with `node drift`) can be run against several hosts, one at a time,
by a mechanism that actually stops at the first failure instead of a human
having to remember to check before moving to the next host. That is not
theoretical: rolling `chiap02` first, by hand, surfaced a stale dispatcher
copy before the same step ever reached `chiap01` or `chiap03`. Had all four
gone at once, three hosts would have been mid-change when the problem
first showed up.

```bash
skcapstone fleet rollout --node chiap01 --node chiap02 --node chiap03 \
  [--repo-root PATH] [--home PATH] [--remote-repo-root PATH] \
  [--apply] [--json] [--strict]

skcapstone fleet rollback --node chiap01 --node chiap02 --node chiap03 \
  [--home PATH] [--repo-root PATH] [--remote-repo-root PATH] \
  [--apply] [--json] [--strict]
```

**`--node` is repeated once per node, and the order given is the order
visited** (`--node chiap01 --node chiap02` visits `chiap01` first). There is
no separate "which nodes" file or discovery step; the operator states the
plan on the command line, which is what makes the dry-run output below a
complete preview of what will happen.

### Dry-run first, always

**Both commands default to a dry run.** Real execution requires `--apply`,
which is the only thing that makes either command write to a host, run a
gate check, or record anything. Without `--apply`, zero network calls and
zero filesystem writes happen, for any node: a dry run is a preview of the
plan, not a live health check dressed up as one. Read the plan before
authorising it. Run against the real fleet, read-only, 2026-09-17:

```
$ skcapstone fleet rollout --node chiap01 --node chiap02 --node chiap03 \
    --node chiap04 --node chiap08 --repo-root ~/work/skcapstone
DRY RUN: previewing rollout across 5 node(s); nothing was executed.
Pass --apply to run this for real.
  [1/5] chiap01	dry run: would record deployment, then git pull, pip install, copy skfleet-rotate.py, and converge on chiap01; not executed
  [2/5] chiap02	dry run: would record deployment, then git pull, pip install, copy skfleet-rotate.py, and converge on chiap02; not executed
  [3/5] chiap03	dry run: would record deployment, then git pull, pip install, copy skfleet-rotate.py, and converge on chiap03; not executed
  [4/5] chiap04	dry run: would record deployment, then git pull, pip install, copy skfleet-rotate.py, and converge on chiap04; not executed
  [5/5] chiap08	dry run: would record deployment, then git pull, pip install, copy skfleet-rotate.py, and converge on chiap08; not executed
```

Every node gets one numbered line, in visiting order, naming exactly what
would run on it. `--json` gives the same plan as a machine-readable object
(`dry_run`, `completed`, `halted_at`, `reason`, `remaining`) for a caller
that wants to render or gate on it programmatically rather than read text.

### Executing for real: `--apply`

With `--apply`, `rollout` visits `--node` arguments in order and, for each
one: records the manifest now in force (so a later rollback has something
to return to -- this happens BEFORE any change, so a failure partway still
leaves a record), then runs the same four steps the activation runbook
documents by hand (`git pull`, `pip install -e .`, copy the dispatcher
script, converge), then gates the node with the existing readiness verdict
plus `node drift`'s own no-unambiguous-drift rule -- no second notion of
"healthy" is invented for this. **The first node that fails to deploy or
fails its gate halts the rollout.** Every later node is left untouched,
never even attempted, and the command names which node stopped it and why.
Illustrative output (rendered from the same code path, not a real `--apply`
run against the fleet -- this document only ever runs the dry-run path
above against real hosts):

```
rollout HALTED after 1/4 node(s).
  [1/4] chiap01	chiap01: deployed and gate passed
  [2/4] chiap02	HALTED: deploy step 'pip_install' failed on chiap02: ImportError: cannot import name 'GATED_EXIT_CODE'
  [3/4] chiap03	not attempted; rollout halted before reaching it
  [4/4] chiap04	not attempted; rollout halted before reaching it
```

### Rolling back

`rollback` has no `--manifest` option: unlike `rollout`, its target is never
chosen on the command line. Each node's "previous manifest" is looked up
per node, at rollback time, from whatever `rollout` actually recorded for
THAT node via `record_deployment` -- never guessed, never reconstructed
from "current minus one commit". **A node with no recorded previous
manifest makes rollback refuse outright, for that node,** with a reason
naming it, exactly like any other halting failure. Illustrative output,
same caveat as above:

```
rollback HALTED after 0/2 node(s).
  [1/2] chiap01	HALTED: no recorded previous manifest for chiap01; rollback refuses to guess or reconstruct one
  [2/2] chiap02	not attempted; rollback halted before reaching it
```

This refusal is deliberate. Before this command, the only rollback practice
in this estate was hand-written `card_events` evidence with no code behind
it at all; refusing to guess replaces that gap with a fact instead of
another guess.

Rollback re-runs the same gate the forward path uses, after every node,
including a successful one. It costs the same time the forward path already
pays, for the same reason: this estate has two documented cases of an
unverified change going unnoticed for a long time precisely because nothing
checked after it landed (a release merged and uninstalled for sixteen
hours; a dispatcher timer active-but-not-enabled for seven weeks). A
rollback is remediation run under pressure on a fleet already known to be
unhealthy, which makes verifying each node MORE important, not a place to
cut. When the gate fails after a rollback, the node's rollback has already
happened (there is no rollback-of-a-rollback); what halts is only the
advance to later nodes, and the reason says "after rollback" so it is never
mistaken for a forward-deploy failure.

### `--strict` and `--json`

Same contract as `node drift`: `--strict` sets a non-zero exit code when the
rollout or rollback halts before every node completes; it does not change
what is printed. `--json` renders the full result (every node's
`deployed`/`ready`/`drift` detail, plus `halted_at`/`reason`/`remaining`) for
a caller that wants to act on it programmatically. A dry run never halts
(nothing runs to fail), so `--strict` on a dry run always exits 0.

## What is still NOT delivered

- **No automatic actuation from a drift finding, a rollout, or a rollback.**
  `fleet node drift` and the readiness gate report; `fleet rollout` and
  `fleet rollback` execute only when a human runs them with `--apply`.
  Nothing here schedules itself, retries itself, or reacts to a finding on
  its own.
- **ATLAS still has no `DEPLOY` authority.** `Seat.ATLAS`'s bound actions
  remain `{OBSERVE, ACTUATE_APPLICATION, CREATE_CARD}`; `Action.DEPLOY`
  remains with the retired `Seat.TANK`, which nothing dispatches to.
  ATLAS's ported release-and-install duty is still recorded as inoperable;
  see
  [seat-charters.md](seat-charters.md#atlas-release-and-install-duty-inoperable-pending-b3)
  for the full accounting, including what granting `DEPLOY` would now
  concretely take. `fleet rollout`/`fleet rollback` are human-invoked CLI
  commands, not a seat capability; running them changes nothing about who
  or what may invoke them.

## Related

- [`docs/fleet/activation-runbook.md`](activation-runbook.md): the manual
  rollout ordering constraints `fleet rollout`'s four deploy steps
  automate, and the constraints it does not remove the need to know.
- [`docs/fleet/seat-charters.md`](seat-charters.md): ATLAS's authority
  boundary, why it cannot deploy today, and what changing that would take.
- [`docs/fleet/model-lane-routing.md`](model-lane-routing.md): why the
  dispatcher script is a separate hand-installed artifact from the package,
  which is why the drift check treats it as its own artifact.
