# Rollout Observability Implementation Plan (nimble-factory Plan B3, phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make "is what we merged actually running" answerable automatically, per
host, without anyone SSHing anywhere.

**Architecture:** Observation before actuation. This phase builds the manifest and
the drift signal and wires the readiness gate. It deliberately does NOT build
staged rollout or rollback, because those need an authority decision this plan
does not make.

**Tech Stack:** Python 3.12, systemd user units, `scripts/fleet/`, `src/skcapstone/fleet/`.

**Spec:** `docs/superpowers/specs/2026-09-16-nimble-factory-design.md` sections
A.4 and A.6, as corrected below.

## Spec corrections, measured 2026-09-17

1. **A.4's premise about ATLAS is wrong.** It describes a seat with "a domain and
   no mechanism" that "produced 8 card events in a week". Measured on chiap08:
   **1,323 events**, 96 percent automated worker-liveness heartbeats, 59
   substantive across 5 cards. `skfleet-atlas.timer` fires every 5 minutes and
   calls `role_dispatch_operation`. ATLAS is not idle; it already runs a working
   card-worker dispatch loop that has nothing to do with rollout.

2. **ATLAS cannot deploy, and this plan does not give it that power.** B2 found
   the fold moved atlas's duty but not its authority: `Seat.ATLAS` is
   `{OBSERVE, ACTUATE_APPLICATION, CREATE_CARD}` with no `DEPLOY`, the
   `approved_artifact_sha256` digest gate is still on the retired tank branch,
   and the ATLAS rail brief forbids deploying. The charter now records the duty as
   inoperable pending this work. **Everything in this phase is observation, which
   `OBSERVE` already permits.**

3. **Every rollout primitive A.4 assumes is absent.** No deployment manifest
   exists in any form. Deployment is a human running `scripts/install.sh`. Nothing
   stages, nothing halts on failure, nothing rolls back by manifest.

4. **The readiness gate exists and has never had a caller.** `skfleet_readiness.py`
   derives `required_env` by AST-parsing the dispatcher, which satisfies A.6's
   "generated, never typed by hand". Zero callers in the repo or on any host,
   confirmed by two reviews weeks apart and again today. It is the only thing
   standing between a deploy and a missing env var, and it runs when a human
   remembers.

5. **The CMDB reconcile path is unwired, not idle.** `cmdb_reconcile_job.py` would
   record `code_version` per host and ships
   `skcapstone-cmdb-reconcile-network.service`, but there is **no `.timer`
   anywhere in the tree**, and it gates on `ConditionPathExists=
   %h/.config/skcapstone/cmdb-network-apply`, a file that does not exist on
   chiap08.

## Why this phase is worth doing, measured today

Three drift incidents found by hand in one session, none of which any automated
signal reported:

```
skmail            three different binaries across five hosts, none matching the
                  repo, because the file was never in pyproject's script-files.
                  No version check could see it: it belongs to no package.
seat units        chiap08 carried skfleet-niobe-shadow.service in a FAILED state,
                  the residue of a documented incident, for weeks.
code drift        chiap01/02/03 sat two merged PRs behind main; chiap04/chiap08
                  further. Found only by SSHing into all five hosts.
```

Version strings were self-consistent per host throughout, so a controller checking
versions would have reported all five healthy during every one of these.

## Global Constraints

- Python 3.12.
- `scripts/fleet/` is outside black and ruff. `src/` and `tests/` are NOT: CI runs
  `black --check src/ tests/` and `ruff check src/`. Run both before committing.
- No em dashes or en dashes anywhere, including comments and commit messages.
- No `Co-Authored-By` trailer you cannot evidence.
- Never write to `~/.skcapstone` from a test. Use a temp directory.
- Reading the live fleet is fine and encouraged. **Writing to a host, restarting a
  unit, or installing anything is out of scope for every task here.** Rollout is a
  separate human decision.
- Commit as soon as a fast check passes. Never gate a commit on a long run.
- When reverting a change to prove a test catches it, print the mutated line back
  FROM DISK before running the suite.
- **Nothing in this plan may land uncalled.** Four instances of tested-but-uncalled
  code were measured today. Every task here either wires what it builds or marks it
  `# intentionally-unwired: <reason>`.

---

### Task 1: The deployment manifest

**Files:**
- Create: `src/skcapstone/fleet/deployment_manifest.py`
- Test: `tests/fleet/test_deployment_manifest.py`

A.6 specifies one artifact pinning what a node must run: `revision`, `git_sha`,
`package_version`, `required_env`, `units`.

**`required_env` must be GENERATED, never typed.** `skfleet_readiness.required_env`
already AST-parses the dispatcher for `_required_lane_target` calls. Reuse it.
A hand-maintained list is what A.6 names as having failed three times.

**Interfaces:**
- Produces: `build_manifest(repo_root, home) -> dict` and
  `write_manifest(path, manifest) -> None`, atomic via tmp plus `os.replace`.

- [ ] **Step 1: Write failing tests.** A manifest carries all five fields;
  `required_env` matches what the readiness gate derives; the write is atomic;
  generating twice from the same inputs is byte-identical.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement.** Derive `git_sha` from the repo, `package_version` from
  the installed distribution, `units` from the shipped systemd tree.
- [ ] **Step 4: Run the tests.**
- [ ] **Step 5: Generate one against this repo READ-ONLY and paste it in your
  report**, so a human can see the real shape.
- [ ] **Step 6: Commit.**

---

### Task 2: Give the readiness gate a caller

**Files:**
- Modify: `scripts/fleet/skfleet_readiness.py`
- Create: `src/skcapstone/data/systemd/skfleet-readiness.service` and `.timer`
- Test: `tests/fleet/test_readiness_wiring.py`

The gate has never run except when a human invoked it. Give it a timer so it runs
on every host and records its verdict where something else can read it.

**Fix its role scoping first.** Run against chiap08 today it reports NOT READY
because `SKFLEET_GATEWAY_URL` is unset for `skfleet-rotate.service`, on a host
where that unit is deliberately `disabled`. A gate that fails for a role the host
does not run teaches operators to ignore it. Check whether a unit is enabled before
asserting its environment, and report skipped units rather than silently passing
them.

**Units ship in two trees.** `systemd/` and `src/skcapstone/data/systemd/` must
stay in step, and `tests/fleet/test_no_dangling_systemd_unit_references.py`
asserts every referenced unit ships. Satisfy it rather than trip it.

- [ ] **Step 1: Write failing tests** for the enabled-aware scoping: a disabled
  unit's env is not asserted and is reported as skipped; an enabled unit's missing
  env still fails.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement the scoping fix.**
- [ ] **Step 4: Add the unit pair in both trees**, writing the verdict to a known
  path under the agent home.
- [ ] **Step 5: Run the dangling-unit test and the full readiness tests.**
- [ ] **Step 6: Commit.**

---

### Task 3: Drift detection, the actual answer to the question

**Files:**
- Create: `src/skcapstone/fleet/rollout_drift.py`
- Test: `tests/fleet/test_rollout_drift.py`

Compare what a host is running against its manifest, and report every difference.

**This is the task that must not rely on version strings.** All five hosts reported
self-consistent versions throughout today's three drift incidents. Compare content:
a digest of each shipped unit file, a digest of the installed dispatcher script, and
the `git_sha`. The `skmail` case is the worked example: three different binaries,
no version difference, because the file belonged to no package.

**Interfaces:**
- Produces: `detect_drift(manifest, home, repo_root) -> list[Drift]` where each
  entry names the artifact, the expected digest, the found digest, and the host.

- [ ] **Step 1: Write failing tests.** A host matching its manifest reports no
  drift; a changed unit file is reported; a changed dispatcher script is reported;
  a missing artifact is reported distinctly from a changed one.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run it READ-ONLY against this checkout and report what it finds.**
- [ ] **Step 5: Commit.**

---

### Task 4: Report drift where a human will see it

**Files:**
- Modify: `src/skcapstone/cli/` (add a command), or extend `skcapstone doctor`
- Test: alongside

Decide and justify: a new `skcapstone fleet drift` command, or a check inside
`skcapstone doctor`. `doctor` already enforces identity consistency and is the
established place operators look, which argues for extending it. A separate command
argues for being runnable per host by a timer without the rest of doctor's cost.

- [ ] **Step 1: Decide, and write the reasoning into your report before coding.**
- [ ] **Step 2: Write failing tests.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run it against the live fleet READ-ONLY and paste the output.**
- [ ] **Step 5: Commit.**

---

### Task 5: Documentation and changelog

- [ ] **Step 1: Find every doc describing deployment or verification.** Report the
  list before editing.
- [ ] **Step 2: Document the manifest, the drift check, and what is still absent:**
  staged rollout, rollback by manifest, and ATLAS's deploy authority. Do not imply
  this phase delivers them.
- [ ] **Step 3: CHANGELOG entry.** `docs-check` fails the build when `src/` changes
  without one. State the measured cause, the three drift incidents, and be explicit
  that this phase observes and does not actuate.
- [ ] **Step 4: Commit.**
