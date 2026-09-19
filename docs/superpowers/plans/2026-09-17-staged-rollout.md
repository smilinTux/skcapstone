# Staged Rollout Implementation Plan (nimble-factory Plan B3, phase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Deploy to one node at a time, verify each before proceeding, halt on the
first failure, and be able to go back.

**Architecture:** A human-invoked mechanism, not an autonomous actuator. It does
NOT grant ATLAS `DEPLOY`. That grant becomes a small separate change once the
mechanism exists and has been exercised, which is the correct order: an actuating
seat with no rollback target is worse than no seat.

**Tech Stack:** Python 3.12, `src/skcapstone/fleet/`, systemd user units.

**Spec:** `docs/superpowers/specs/2026-09-16-nimble-factory-design.md` A.4.

## What phase 1 already provides

```
deployment_manifest.py  build_manifest / write_manifest, revision is a content
                        digest so identical state compares equal
rollout_drift.py        detect_drift reporting missing, changed,
                        enablement_mismatch, failed. Verified quiet on a
                        correctly configured host.
skfleet_readiness.py    scope-aware, has a timer, writes a node-scoped verdict
```

So the verification half of "deploy, then gate it" is built and proven. Phase 2
adds the sequencing and the reverse.

## Why one node at a time, measured

Today's manual roll of four hosts is the worked example. Rolling chiap02 first
surfaced a stale dispatcher copy before the same step touched chiap01 or chiap03.
Had all four gone at once, three hosts would have been mid-change when the first
problem appeared. The estate has also paid for the opposite of rollback: a
release sat merged and uninstalled for sixteen hours with nothing detecting it,
and the only existing rollback practice is a hand-written `card_events`
convention with no backing code.

## Global Constraints

- Python 3.12.
- CI runs `black --check src/ tests/` and `ruff check src/`. Run both before
  committing, and run pytest before committing, not only lint.
- No em dashes or en dashes anywhere, including comments and commit messages.
- No `Co-Authored-By` trailer you cannot evidence.
- Never write to `~/.skcapstone` from a test. Use a temp directory.
- **Reading the live fleet is fine. Writing to a host is NOT part of any task
  here.** Every task ships a mechanism plus a dry run. Actually rolling the fleet
  with it is a separate human decision, taken after review.
- Nothing may land uncalled. Six instances of tested-but-uncalled code have been
  found in this codebase. Wire it or mark it `# intentionally-unwired: <reason>`.

---

### Task 1: Record what a node ran, so there is something to go back to

**Files:**
- Create: `src/skcapstone/fleet/rollout_history.py`
- Test: `tests/fleet/test_rollout_history.py`

Rollback needs a previous state. Today nothing records one: `lifecycle_seats` has
a rollback for its own config files only, and the wheel-level convention exists
solely as hand-written evidence in `card_events` with no code behind it.

Record, per node, the manifest that was in force before each change, append-only,
so "the previous manifest" is a fact rather than a reconstruction.

**Interfaces:**
- Produces: `record_deployment(home, manifest) -> None` and
  `previous_manifest(home) -> dict | None`.

- [ ] **Step 1: Write failing tests.** Recording twice yields the first as
  previous; the first record has no previous; the store is append-only; a
  corrupt entry does not destroy the history.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement**, node-scoped under the agent home the same way Task 2
  of phase 1 scoped its verdict, because `~/.skcapstone` is ONE Syncthing folder
  and an unscoped path lets five hosts overwrite each other.
- [ ] **Step 4: Run the tests. Commit.**

---

### Task 2: Staged rollout with halt on first failure

**Files:**
- Create: `src/skcapstone/fleet/staged_rollout.py`
- Test: `tests/fleet/test_staged_rollout.py`

Deploy to one node, gate it, and only then proceed. On the first failure, stop
and report which node failed and why. Do NOT continue to the remaining nodes.

**The gate is the existing readiness gate plus `detect_drift`.** Do not invent a
second notion of healthy. A node passes when readiness reports READY and drift
reports no unambiguous findings.

**Interfaces:**
- Consumes: `build_manifest`, `detect_drift`, the readiness verdict, Task 1's
  `record_deployment`.
- Produces: `plan_rollout(nodes, manifest) -> RolloutPlan` and
  `execute_rollout(plan, *, dry_run=True) -> RolloutResult`.

**`dry_run` defaults to True.** A rollout tool whose default is to act is the
wrong default for a first version; the backfill script in an earlier plan had the
same property and it was the right call there too.

- [ ] **Step 1: Write failing tests.** A plan visits nodes in a deterministic
  order; a dry run mutates nothing; a failure at node 2 of 4 halts and leaves
  nodes 3 and 4 untouched; the result names the failing node and the reason.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run a DRY RUN against the real fleet and paste the output.**
- [ ] **Step 5: Commit.**

---

### Task 3: Rollback to the previous manifest

**Files:**
- Modify: `src/skcapstone/fleet/staged_rollout.py`
- Test: alongside

Going back to the manifest Task 1 recorded. Same staging and the same halt rule:
a rollback that fails halfway is worse than one that refuses to start.

**Decide and justify:** whether rollback re-runs the gate after each node. It
costs time; skipping it means a rollback can leave a node unverified, which is
how the estate ended up with sixteen hours of undetected drift.

- [ ] **Step 1: Write failing tests**, including that rollback with no recorded
  previous manifest refuses clearly rather than guessing.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Dry-run it and paste the output. Commit.**

---

### Task 4: Surface it, and document what is still not automatic

**Files:**
- Modify: `src/skcapstone/fleet/cli.py`
- Docs and CHANGELOG

Add the commands as siblings of `fleet node drift`. Then document plainly:
this is human-invoked. ATLAS still holds `OBSERVE`, `ACTUATE_APPLICATION` and
`CREATE_CARD`, not `DEPLOY`, and its release duty remains recorded as inoperable.
Say what granting that authority would take, so the decision is small and
concrete rather than open-ended.

- [ ] **Step 1: Add the commands, dry run by default.**
- [ ] **Step 2: Find every doc describing deployment. Report the list first.**
- [ ] **Step 3: Update them and add a CHANGELOG entry.**
- [ ] **Step 4: Commit.**
