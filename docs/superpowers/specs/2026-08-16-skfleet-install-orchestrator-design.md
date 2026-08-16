# skfleet install: profile-aware stack installer (orchestrator)

**Date:** 2026-08-16
**Status:** design, approved-for-planning
**Epic:** node-roles-install-profiles (`3bbf39ea`)

## Problem

Installing "the whole sk* stack" on a node is a manual, per-repo chore. The
profile layer that says *what a node should run* is already built (epic
`3bbf39ea`): per-role manifests at `deploy/fleet-objects/profile/*.json` declare
`packages` and `units` as `{required, allowed, mustNot}`, and `fleet/profile_doctor.py`
computes the drift between a manifest and a node's live inventory. What is missing
is **actuation**: nothing reads that drift and installs the missing pieces.
`fleet/actuation.py` only `start`/`restart`s units that are already installed.

Today, closing the gap means a human runs `skcapstone/scripts/install.sh`,
`skchat/systemd/install.sh`, the skcomms/skmemory/skwhisper/capauth installers by
hand, in the right order, and hopes it matches the role. This spec adds the one
missing verb that does it for you, idempotently, driven by the profile.

## Goal and non-goals

**Goal.** A `skfleet install` verb that reads this node's bound profile, reports
the drift (`--check`), and closes every `missing_required` package/unit (`--apply`)
by driving the existing per-repo installers as backends. Idempotent, safe by
default, and wrappable by the AI/GUI wizard (`gui_installer.py`) which stays the
UX layer on top.

**Non-goals (explicitly out of scope for this spec).**
- The AI/question-asking wizard UX. It wraps this verb; it is not built here.
- Authoring new profiles or changing the profile schema.
- Remote/cross-node install (this installs the LOCAL node to its own role;
  fan-out to other nodes is a later verb built on this one).
- Uninstalling `mustNot`/`unexpected` units. This verb only ADDS
  `missing_required`; removing things stays a separate, human-gated action.

## Approach (A: orchestrator, chosen)

`skfleet install` is an orchestrator, not a re-implementation. Its plan comes
from `profile_doctor.diff()`; each `missing_required` finding is closed by
invoking the repo that owns that unit/package as a **backend**. This reuses every
proven per-repo installer and keeps a single source of truth (the manifests).

Rejected alternatives: (B) `skfleet` materializes unit files and pip-installs
directly from the manifest, which duplicates and drifts from the per-repo
installers; (C) a thin bash wrapper that calls each installer in order, which is
not check-aware, not idempotent as a whole, and ignores `profile_doctor`.

## Components

All new code lands in `skcapstone/src/skcapstone/fleet/`, beside the existing
profile machinery.

### 1. `fleet/installer.py` (new)

The orchestrator. Pure-ish: takes a `DriftReport` (from `profile_doctor`) and a
backend registry, produces an ordered `InstallPlan`, and (on apply) executes it,
returning an `InstallResult` per finding.

- `plan(drift: DriftReport, registry) -> InstallPlan`: turns `missing_required`
  packages/units into ordered `InstallStep`s. Order comes from a small, explicit
  dependency table (section "Ordering"): venv/packages first, then identity/auth
  (capauth-authz), then transport (skcomms), then core (skcapstone), then the
  skchat plane, then per-agent templated units. Steps for already-satisfied or
  `mustNot`/`allowed`-only items are never generated.
- `apply(plan, *, dry_run, enable, start) -> list[InstallResult]`: executes each
  step through its backend. A step failure is captured in its `InstallResult`
  and does NOT abort the run; remaining independent steps still run. Steps whose
  dependency step failed are marked `skipped(reason=...)`.

### 2. `fleet/install_backends.py` (new)

The registry that maps a package/unit name to the command that installs it,
following the reconcile contract below. Each backend is a small adapter, not a
copy of the installer logic:

- `packages.required` -> `skcapstone/scripts/install.sh` (the venv + pip-in-dep-order
  installer). One backend closes all missing packages in a single call.
- skchat-plane units (`skchat-*`, `livekit-server`, `jarvis-heartbeat`, ...) ->
  `skchat/systemd/install.sh` with the matching `--enable`/`--start` flags. This
  installer is already the gold-standard reconciler; we call it, we do not re-do it.
- `skcomms-*` -> `skcomms/scripts/bootstrap.sh --no-service` for install, enable
  separately (honours its `identity check --strict` gate).
- `skmemory-*@` -> `skmemory/scripts/install-systemd.sh`.
- `skwhisper@` -> `skwhisper install --agent <a>`.
- `capauth-authz` -> `capauth/deploy/capauth-service` deploy (docker-compose) OR
  its systemd unit; the backend picks the form the manifest declares.
- A `UNSUPPORTED` fallback: a `missing_required` unit with no registered backend
  is reported as `needs_manual` with the unit name, never silently skipped.

The registry is data (`{glob_or_name: Backend}`) so a new subsystem is one entry.

### 3. `skfleet install` CLI verb (extend `fleet/cli.py`)

```
skfleet install [--role R] [--check | --apply]
                [--dry-run] [--enable] [--start]
                [--only NAME ...] [--json]
```

- `--check` (default): print the drift (delegates to `profile_doctor`), exit 0 if
  no `missing_required`, else exit 1. Touches nothing.
- `--apply`: build and run the plan. Default is copy + `daemon-reload` only;
  `--enable` and `--start` are opt-in (never restart a running unit).
- `--role`: override the node's bound role (default: the role from the node's
  admission object; error if unbound and not given).
- `--only NAME ...`: restrict to specific units/packages (for targeted repair).
- `--json`: machine-readable plan/result, the contract the GUI/AI wizard consumes.

## Reconcile contract (inherited from `skchat/systemd/install.sh`)

Every backend and the orchestrator honour these, so a second run is a near no-op:

1. **Content-compare before write** (`cmp -s`): a unit is written only if absent
   or different; report `[=ok]` / `[wrote]` / `[would-write]` (dry-run).
2. **Copy is separate from activate:** default copies unit files + `daemon-reload`;
   `--enable`/`--start` are explicit; `--start` never restarts a running unit.
3. **`--dry-run` prints the plan and touches nothing.**
4. **`systemd-analyze --user verify`** each written unit before reload.
5. **Secret preflight:** a required `EnvironmentFile` that is missing is a WARN in
   the result (not a hard fail), so the operator sees it and the run continues.

## Safety

- **Only `missing_required`.** `mustNot` and `unexpected` are never actuated
  (removal stays human-gated). `allowed`-but-absent is not installed.
- **Freeze gate + per-node `actuate` opt-in:** `--apply` refuses if the fleet is
  frozen or this node has not opted into actuation, reusing the existing
  `converge.py`/`actuation.py` gates. `--check` and `--dry-run` are always allowed.
- **Idempotent + resumable:** a re-run after a partial failure only closes what is
  still missing.

## Data flow

```
bound role -> load profile manifest (deploy/fleet-objects/profile/<role>.json)
           -> node live inventory (fleet/nodeinventory.py)
           -> profile_doctor.diff() -> DriftReport
           -> installer.plan(drift, registry) -> InstallPlan (ordered steps)
  --check:  print report, exit code from missing_required count
  --apply:  installer.apply(plan) -> per-step InstallResult -> print/JSON report
```

## Ordering

A small explicit table (not encoded in the units, which are loose). Steps run in
this order; within a tier, order is stable by name:

1. `~/.skenv` venv + `packages.required` (skcapstone install.sh)
2. identity/auth: `capauth-authz`
3. transport: `skcomms-*`
4. core: `skcapstone*`, `sknoded`, `skgateway`
5. skchat plane: daemons, then `livekit-server`/`coturn`, then tts/nostr, then
   webui/call
6. per-agent templated: `skmemory-*@`, `skwhisper@`, `cloud9-daemon@`

## Error handling

- A backend nonzero exit -> `InstallResult(status=failed, stderr_tail=...)`; the
  run continues with independent steps; dependents are `skipped`.
- Unknown unit (no backend) -> `needs_manual`, surfaced in the summary.
- Missing secret file -> `warn`, step still attempts copy (activation may later
  fail loudly, which is correct).
- Final exit code: 0 if every `missing_required` is now satisfied, else 1, with a
  summary of failed/skipped/manual items.

## Testing (TDD)

- `installer.plan`: given a fixture `DriftReport` (missing units+packages across
  tiers) and a fake registry, asserts the ordered step list, that satisfied /
  `mustNot` / `allowed` items produce no step, and that `--only` filters.
- `installer.apply`: fake backends that record calls; asserts dry-run touches
  nothing, a backend failure isolates to its step + skips dependents, and the
  copy-vs-enable split (no enable/start unless flagged).
- Backend registry: name/glob -> backend resolution, and the `UNSUPPORTED`
  fallback path.
- CLI `--check` against a fixture profile + fake node inventory: exit code and
  report shape; `--json` schema is stable (wizard contract).
- Reconcile semantics reuse: a second `apply` over an already-satisfied node
  produces an all-`[=ok]` plan (idempotence).

Backends shelling out to real installers are covered by a thin integration test
gated behind an env flag (they need the real repos), not the default unit run.

## Folded-in cleanups (block correctness, small)

- **Stale unit names in `skcapstone/scripts/install.sh`:** it references units that
  do not exist in `skchat/systemd/units` (`skchat-lumina-bridge`,
  `skchat-bridges.target`). Fix so the packages backend does not try to enable
  phantom units.
- **Duplicate profile mechanism in `skos/install/`:** `skos` ships its own
  `planner/profiles/provisioner`. This spec does NOT subsume it here (scope), but
  records the duplication and adds a follow-up card to reconcile it, so we do not
  grow a third profile store.

## Wrappability (the wizard contract)

The GUI/AI wizard (`gui_installer.py`, `installer/build.py`) determines the role
and any per-node choices by asking the operator, then calls
`skfleet install --role <R> --apply --json` and renders the returned plan/result.
The wizard owns questions and presentation; this verb owns the deterministic,
testable actuation. That is the "basic installer we wrap around."
