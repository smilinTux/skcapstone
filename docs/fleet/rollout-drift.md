# Rollout drift: is what we merged actually running (nimble-factory Plan B3, phase 1)

**Spec:** `docs/superpowers/specs/2026-09-16-nimble-factory-design.md`, sections
A.4 and A.6, as corrected in
`docs/superpowers/plans/2026-09-17-rollout-observability.md`.
**Code:** `src/skcapstone/fleet/deployment_manifest.py`,
`scripts/fleet/skfleet_readiness.py`, `src/skcapstone/fleet/rollout_drift.py`,
`skcapstone fleet node drift` (`src/skcapstone/fleet/cli.py`).

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
`os.replace`). Nothing in this phase publishes a manifest to a fixed,
well-known path on a host; `build_manifest` is a pure function a caller
supplies `repo_root`/`home` to. `skcapstone fleet node drift` (below) is
that caller.

## 2. The readiness verdict

`scripts/fleet/skfleet_readiness.py` checks, before a rotate host's
dispatcher would run, that every `required_env` variable is actually set and
every seat module actually imports under the target interpreter. It now has
a caller: `skfleet-readiness.service` / `.timer`, installed from
`systemd/skfleet-readiness.service` (mirrored byte-identical into
`src/skcapstone/data/systemd/`), running every 15 minutes
(`OnBootSec=5min`, `OnUnitActiveSec=15min`).

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
content digest of the installed dispatcher script, the installed
distribution's embedded git commit (not its semantic version, which is
exactly what let the `skmail` incident hide), and whether each in-scope
unit's enabled state agrees with its active state. Local-node-only, like
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
| `changed` | The artifact is installed, but its content differs from what the manifest pins. |
| `enablement_mismatch` | A unit's `ActiveState` and enabled (`UnitFileState`) disagree, e.g. active but not enabled. This is the timer-enablement incident above: no content digest can catch it, because the unit *file* was correct. |
| `missing` | The artifact was not found at all. |
| `git_sha` (artifact) | The installed distribution's embedded commit differs from (or is entirely absent from) what the manifest expects. |

**`changed`, `enablement_mismatch`, and any `git_sha` finding are shown by
name in the default text output: they are unambiguous regardless of what
this host's role is.** A `missing` unit or dispatcher-script finding is
different: this estate has no per-host role manifest, so "this host's role
never installs that unit" cannot be told apart from "a rollout should have
installed it and did not." Run against this checkout, most `missing`
findings are the former: a workstation or a seat-only host legitimately
never carries every unit the full package ships. Printing all of them by
default buried the six lines that actually mattered under fourteen that did
not, which is exactly the kind of noise that gets a check ignored within a
week. So the default text output lists every unambiguous finding by name and
folds `missing` findings into one summary line instead:

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
  Re-run with --json or --strict to see each by name.
```

`--json` and `--strict` are unaffected by this split: both see and act on
**every** finding, `missing` included. `--json` emits the full list, always;
`--strict` exits 1 if any finding at all is present, missing findings
included, so a caller that wants to gate on absence (rather than merely
report it) still can.

A clean host reports:

```
$ skcapstone fleet node drift
node-chiap01: no drift (matches b421c0d1)
```

## What this phase does NOT deliver

Observation, not actuation. Concretely, none of the following exist yet:

- **No staged rollout.** Nothing here stages a release across a subset of
  hosts, waits, or halts on failure. Deployment is still a human running
  `scripts/install.sh`.
- **No rollback by manifest.** The manifest records what a node should run;
  nothing consumes it to revert a node to a prior pinned state.
- **No automatic actuation from a drift finding.** `fleet node drift` and the
  readiness gate report; neither one restarts, reinstalls, or converges
  anything. A human (or a future phase) still reads the report and acts.
- **ATLAS still has no `DEPLOY` authority.** `Seat.ATLAS`'s bound actions are
  `{OBSERVE, ACTUATE_APPLICATION, CREATE_CARD}` only; `Action.DEPLOY` remains
  with the retired `Seat.TANK`. ATLAS's ported release-and-install duty is
  recorded as inoperable pending this and further work; see
  [seat-charters.md](seat-charters.md#atlas-release-and-install-duty-inoperable-pending-b3).
  Everything documented above is observation, which `OBSERVE` already
  permits; nothing here changes that authority boundary.

## Related

- [`docs/fleet/activation-runbook.md`](activation-runbook.md): the rollout
  ordering constraints these tools help verify after the fact.
- [`docs/fleet/seat-charters.md`](seat-charters.md): ATLAS's authority
  boundary and why it cannot deploy today.
- [`docs/fleet/model-lane-routing.md`](model-lane-routing.md): why the
  dispatcher script is a separate hand-installed artifact from the package,
  which is why the drift check treats it as its own artifact.
