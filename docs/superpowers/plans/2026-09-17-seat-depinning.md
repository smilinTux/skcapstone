# Seat Depinning Implementation Plan (nimble-factory Plan B2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let any host in an estate run any seat, replacing a static elected host
with the claim fence that already prevents double-dispatch, and fold `tank` into
`atlas`.

**Architecture:** The depinning mechanism already exists and is unused. Nothing
new is built; a second exclusion mechanism is retired in favour of the one that
is already proven in production.

**Tech Stack:** Python 3.12, `scripts/fleet/skfleet-rotate.py` (a script),
`src/skcapstone/lifecycle_seats.py`, `src/skcapstone/seat_cycle_entrypoint.py`.

**Spec:** `docs/superpowers/specs/2026-09-16-nimble-factory-design.md` sections
3.6 and A.5, as corrected below.

## Spec corrections, measured 2026-09-17

The spec is accurate about intent and wrong about several facts. Corrected here
rather than in the spec, so the spec stays the record of what was decided.

1. **The estate is the boundary, and the spec is right about `chiap08`.** Each
   estate has its own control plane: the chi estate names `chiap08` on all five
   hosts (`lifecycle-six-seat-v1`); `noroc2027` names itself
   (`nor-lifecycle-six-seat-v1`). An earlier reading of this plan called the spec
   stale by comparing the NOR file against a spec describing CHI. It is not
   stale. Depinning means host-agnostic WITHIN an estate.

2. **CORRECTION WITHDRAWN. The spec's "seven to five" was right, and this plan
   was wrong to dispute it.** There are two distinct rosters by design:

   ```
   LIFECYCLE_SEATS         link mero seraph niobe atlas         which seats the
                                                                control plane runs
   seat_boundaries.Seat    atlas jarvis link mero niobe          the authority model,
                           seraph tank                          who may do what
   ```

   `jarvis` and `tank` are members of the authority model without being lifecycle
   seats. So 3.6's count of seven refers to `seat_boundaries.Seat`, which genuinely
   has seven members, and folding tank leaves six there while leaving five in
   `LIFECYCLE_SEATS`. An earlier revision of this plan asserted no jarvis seat
   existed and called the spec wrong; that was the second time this effort
   mistakenly accused this spec of being stale, after comparing the NOR control
   plane against a spec describing CHI.

   Practical consequence: `seat_boundaries.Seat.TANK` is correctly RETAINED.
   Tank is no longer a running seat, but it remains a known actor whose historical
   board actions must still resolve against the authority model.

3. **The depinning mechanism already exists.** `seat-placement.json`, read by
   `_load_seat_placement` (`skfleet-rotate.py:2667`), maps each seat to a LIST of
   hosts and validates every entry against `ROTATION_HOSTS`. It supports multiple
   hosts per seat today. Every seat simply lists one.

4. **Two of six seats are already on the fleet path.** `seat-placement.json` in
   the chi estate lists `link`, `mero`, `niobe`, `seraph`, all `['chiap08']`.
   `atlas` and `tank` are ABSENT, so `_seat_provisioned` is false for them and
   they report `seat-unprovisioned`; they run as systemd timers instead. This
   matches their unit history: `niobe` and `seraph` show `last=never` as units.

5. **19 unit files reference the seats, not 12.** Including a
   `skfleet-seat-cycle` pair and a distinct `skfleet-niobe-live` carrying
   `SKFLEET_TARGET=3` and an `OnFailure` alert.

6. **Nothing in the repository writes `seat-placement.json`.** It is read by the
   dispatcher and hand-maintained. A.6 says a hand-maintained prerequisite list
   is "the thing that failed three times already", which applies to this file.

## The invariant this plan must not break

`lifecycle_seats.load_seat_control_plane` manufactures the pin on every converge:

```python
value["seats"] = {seat: [host] for seat in sorted(LIFECYCLE_SEATS)}
```

Its docstring defends `active_host` deliberately:

> "it is an estate-wide ELECTION, not host-local truth. Exactly one host per
> estate runs the six seats, and a per-host answer would let two hosts both claim
> the seat and dispatch the same cards twice."

That is correct today. A.5's answer is that the CardStore claim fence, already
exact-revision fenced and proven in production, replaces the election.

**Therefore the ordering below is safety-critical and must not be reordered.**
The claim fence must demonstrably gate a seat cycle BEFORE any host pin is
relaxed. Relaxing the pin first, even briefly, means two hosts dispatch the same
cards twice, which is the precise failure the docstring names. Plan A shipped a
state with no exit by building one half of a pair; this is the same hazard with a
worse blast radius, because it is live dispatch rather than a stuck card.

## Global Constraints

- Python 3.12.
- `scripts/fleet/` is outside black and ruff. `src/` and `tests/` are NOT: CI runs
  `black --check src/ tests/` and `ruff check src/`. Run both before committing.
- No em dashes or en dashes anywhere, including comments and commit messages.
- No `Co-Authored-By` trailer you cannot evidence.
- `skfleet-rotate.py` is a script; tests reach it by AST extraction. It DOES
  import from `skcapstone`, so shared logic may live in `src/` and be imported.
- Never mutate `~/.skcapstone`. Use a temp directory in tests.
- Commit as soon as a fast check passes. Never gate a commit on a long run.
- When reverting a change to prove a test catches it, print the mutated line back
  FROM DISK before running the suite.
- **Do not enable multi-host placement on any live estate.** Every task here is
  code and tests only. Rolling it out is a separate, human decision.

---

### Task 1: Prove the claim fence actually excludes a second host

**Files:**
- Create: `tests/test_seat_cycle_exclusion.py`

**Why first:** every later task removes a guard on the strength of this one. If
the claim fence does not exclude a concurrent second host for a seat cycle, the
whole plan is unsafe and must stop here.

This task adds NO production code. It characterises existing behaviour.

- [ ] **Step 1: Find how a seat cycle claims work.** Read
  `seat_cycle_entrypoint.py` and the `ONLY_SEAT` path in `skfleet-rotate.py`.
  Report what the unit of exclusion actually is: a card claim, a lock, or
  nothing. Do not assume it is a claim because this plan says so.
- [ ] **Step 2: Write a test that simulates two hosts** running the same seat
  cycle against one CardStore, and asserts exactly one wins.
- [ ] **Step 3: Run it.** If it FAILS, stop and report: the fence does not hold
  and Tasks 2 to 5 must not proceed. If it passes, record what mechanism made it
  pass.
- [ ] **Step 4: Commit.**

---

### Task 2: Add atlas and tank to the placement manifest path

**Files:**
- Modify: `scripts/fleet/skfleet-rotate.py`
- Test: `tests/test_seat_placement.py`

`atlas` and `tank` are absent from `seat-placement.json`, so `_seat_provisioned`
returns false and they report `seat-unprovisioned`. Before either can move off a
timer, the fleet path has to be able to run them at all. Measured: the manifest
on chiap08 lists only link, mero, niobe and seraph, is dated 2026-09-08, and is
written by nothing in this repository.

**Do NOT hardcode a seat roster or a seat count.** Task 3 folds `tank` into
`atlas`, so any literal six in this task's code or tests is something Task 3 then
has to rip out, and a second copy of the roster inside the dispatcher is the same
two-copies drift that has already cost this effort time twice. Provision whatever
`LIFECYCLE_SEATS` contains, so the fold flows through without touching your work.

- [ ] **Step 1: Write failing tests** for a placement manifest covering every seat
  in `LIFECYCLE_SEATS`, asserting `_seat_provisioned` is true for each and that an
  unknown seat name is still rejected. Derive the roster; do not spell it out.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Make the fleet path accept every seat in the roster.** Change no
  host lists.
- [ ] **Step 4: Run the tests plus `pytest tests/ -k skfleet -q`; verify the
  script parses.**
- [ ] **Step 5: Commit.**

---

### Task 3: Fold tank into atlas

**Files:**
- Modify: `src/skcapstone/lifecycle_seats.py`, `src/skcapstone/data/lifecycle-seat-profiles.json`
- Test: `tests/test_lifecycle_seats.py`

Spec 3.6: ATLAS is Operations per ADR-0005 and absorbs tank, whose real activity
(releases, rollback rehearsals, 26 events) is a subset of it. The ATLAS name
survives because ADR-0005 names it and it carries its own constitution. Port
tank's on-charter behaviour; do NOT carry forward atlas's off-charter behaviour
(editing source).

**Interfaces:**
- `LIFECYCLE_SEATS` becomes five seats. `load_seat_control_plane` and
  `load_lifecycle_seat_profiles` both assert the roster matches exactly, so both
  assertions and the packaged data files must change together.

- [ ] **Step 1: Enumerate every place the six-seat roster is asserted or
  listed.** Report the list before editing. There are at least the two loaders,
  the packaged `seat-control-plane.json` and `lifecycle-seat-profiles.json`, and
  the systemd units. Do not trust that count.
- [ ] **Step 2: Write failing tests** asserting the roster is five seats, that
  `tank` is rejected as a seat name, and that atlas's profile carries the ported
  release and rollback duties.
- [ ] **Step 3: Run them, confirm they FAIL.**
- [ ] **Step 4: Implement**, including removing the `skfleet-tank` unit pair.
- [ ] **Step 5: Run `pytest tests/ -k "seat or lifecycle" -q`.**
- [ ] **Step 6: Commit.**

---

### Task 4: Move the timer refusal from active_host to placement membership

**Files:**
- Modify: `src/skcapstone/seat_cycle_entrypoint.py`
- Test: `tests/test_seat_cycle_entrypoint.py`

`seat_cycle_entrypoint.py:172-185` raises three separate `ValueError`s: missing
`active_host` or `revision`; `active_host` not in the seat's host list; and the
local machine not matching `active_host`. The refusal is correct and must stay a
refusal. What changes is the question it asks: from "am I THE elected host" to
"am I ONE of the hosts permitted to run this seat".

**Do not delete `active_host` in this task.** Leave it in the record and ignored
by this path, so a rollback is a one-line revert rather than a data migration.

- [ ] **Step 1: Write failing tests:** a host listed for the seat proceeds; a host
  not listed is refused with a clear message; an empty host list is refused; a
  malformed record is still refused.
- [ ] **Step 2: Run them, confirm they FAIL for the right reason.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Mutation-check the refusal.** Disable it, print the mutated line
  back FROM DISK, confirm the refusal tests go red, restore.
- [ ] **Step 5: Run `pytest tests/ -k seat -q`.**
- [ ] **Step 6: Commit.**

---

### Task 5: Stop manufacturing the single-host pin

**Files:**
- Modify: `src/skcapstone/lifecycle_seats.py`
- Test: `tests/test_lifecycle_seats.py`

`load_seat_control_plane` rewrites every seat to `[host]` on every converge, so
any multi-host placement is erased the next time convergence runs. This is the
line that makes the pin permanent.

- [ ] **Step 1: Write a failing test** asserting a record that already lists two
  hosts for a seat survives a converge unchanged.
- [ ] **Step 2: Run it, confirm it FAILS** because the list is rewritten to one.
- [ ] **Step 3: Implement.** Preserve existing host lists; fall back to the local
  host only when a seat has no hosts at all, which keeps a fresh estate working.
- [ ] **Step 4: Update the docstring.** It currently defends `active_host` as an
  election that prevents double dispatch. State what replaced it and why, citing
  Task 1's evidence. A docstring that argues for the behaviour you just removed
  is worse than no docstring.
- [ ] **Step 5: Run `pytest tests/ -k "seat or lifecycle" -q`.**
- [ ] **Step 6: Commit.**

---

### Task 6: Generate the placement manifest instead of hand-maintaining it

**Files:**
- Modify: `src/skcapstone/lifecycle_seats.py`
- Test: `tests/test_seat_placement_generation.py`

Nothing in the repository writes `seat-placement.json`. A.6 states that a
hand-maintained prerequisite list is "the thing that failed three times already".
The dispatcher fails closed when the file is missing or malformed
(`manifest-unavailable`, `manifest-schema`), so an absent file silently
un-provisions every seat. That is how `atlas` and `tank` ended up unprovisioned.

- [ ] **Step 1: Write failing tests** for a generator that emits a schema-1
  manifest for the five seats, validates every host against the estate's
  rotation hosts, and is idempotent.
- [ ] **Step 2: Run them, confirm they FAIL.**
- [ ] **Step 3: Implement**, writing atomically (tmp plus `os.replace`, same
  directory), matching the existing `_atomic_write` in this module.
- [ ] **Step 4: Verify against the real estate READ-ONLY:** generate to a temp
  path and diff against the live `seat-placement.json` on a chi host. Report the
  diff. Do not write to `~/.skcapstone`.
- [ ] **Step 5: Commit.**

---

### Task 7: Documentation and changelog

- [ ] **Step 1: Find every document describing the elected-host model.** Report
  the complete list before editing. `grep -rn "active_host" docs/` is a start,
  not an answer.
- [ ] **Step 2: Update them**, including the cold-start constraint: niobe keeps a
  minimal timer presence on at least two hosts as supervisor of last resort,
  because dispatch cannot bootstrap through the thing it dispatches.
- [ ] **Step 3: Add a CHANGELOG entry.** `docs-check` FAILS the build when `src/`
  changes without one. State the measured cause and do not overstate: this
  enables multi-host placement, it does not by itself move any seat.
- [ ] **Step 4: Commit.**
