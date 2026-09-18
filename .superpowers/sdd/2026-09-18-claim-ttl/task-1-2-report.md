# Task 1 + Task 2 implementation report

Worktree: `/home/cbrd21/skworld-worktrees/claim-ttl-skcapstone`
Branch: `feat/claim-ttl-heartbeat`

## Commits

- `4011dd06` feat(fleet): pure claim-expiry decision module (Task 1)
- `e8d3224e` feat(fleet): observe held claims and owner idleness (Task 2)

Both commits end with `Agent: Claude Sonnet 5 (subagent, Tasks 1-2)`. No
`Co-Authored-By` footer added.

## What was implemented

### Task 1 - `src/skcapstone/fleet/claim_expiry.py` (new file)

- `ClaimObservation` and `ExpiryVerdict` frozen dataclasses, exactly as
  specified in the brief.
- `ttl_seconds_from_env(env)`: parses `SKFLEET_CLAIM_TTL_H`, defaults to
  48h, rejects non-positive/unparseable values by falling back to default.
- `mode_from_env(env)`: parses `SKFLEET_CLAIM_TTL_MODE`, strips/lowercases,
  defaults to `"off"` for anything not in `{"off", "report", "enforce"}`.
- `evaluate(observations, *, now, ttl_seconds)`: pure decision function.
  Refusal order: no claim_revision -> no-claim-revision; no
  last_owner_event_at -> no-owner-activity; timestamp in the future ->
  future-timestamp; idle <= ttl_seconds -> within-ttl (boundary is
  exclusive of reclaim, i.e. exactly-at-ttl is NOT reclaimable); otherwise
  idle-beyond-ttl and reclaimable=True. Never raises on malformed input.

Test file: `tests/fleet/test_claim_expiry.py`, copied verbatim from the
brief's Step 1 code block. 11 tests, all passing.

### Task 2 - `observe(home: Path) -> list[ClaimObservation]` (appended to
same file)

- Reads `<home>/cards/<card_id>/events/*.jsonl` directly (no CardStore
  import), per the explicit design note in my task brief: the deadline
  needs the last event the OWNER wrote, which `CardStore.fold()` does not
  retain, and adding a `skcoord` import to "satisfy a reviewer" would
  contradict the design.
- Parses each line as JSON, skips corrupt lines (`json.JSONDecodeError` is
  a `ValueError` subclass, caught and skipped) and non-dict payloads.
- Sorts events by `(seq or 0, parsed_ts)` before replay.
- Ownership replay is sequential set/clear, not claim-count vs
  release-count: a `claim` event with a truthy `owner` sets
  `owner`/`revision` (taking the newest `claim_revision` on a same-owner
  re-claim); a `release_claim`/`unassign` clears both to `None`. This
  is the design that correctly handles the `f17d9e32`-style case (2
  claims, 1 release, still held) described in my task brief, since each
  action simply overwrites state rather than being tallied.
- A `complete`/`void`/`archive` action anywhere in the log marks the card
  terminal and it is excluded, matching "ignores a completed card".
- `last_owner_event_at` is the max timestamp across all events where
  `writer == owner` or `owner == owner` (covers both the claim event,
  which carries `owner`, and later `move`/`describe`/etc. events, which
  carry `writer`).
- Missing `<home>/cards` tree returns `[]` rather than raising.

Test file: `tests/fleet/test_claim_expiry_observe.py`, copied verbatim
from the brief's Step 1 code block. 6 tests, all passing.

## Where the brief was inconsistent, and what I did

The Task 2 brief's Step 3 **prose** says: "Prefer the real fold. Import
`CardStore` from `skcoord.card_store` inside the function so the module
stays importable without skcoord present, and fall back to a local
replay when the fold is unavailable." But the **code block immediately
below that prose** contains no `CardStore` import and no fold call at
all - it is a pure direct-replay implementation, exactly matching what
my task brief's context note 1 says is the actual design intent
("Task 2's `observe()` deliberately reads the event log directly rather
than calling `CardStore.fold()` ... Do not add a skcoord import").

I implemented the code block as given (direct replay only, no skcoord
import anywhere), since that matches both the actual sample code and the
explicit design note from my dispatcher. The prose sentence describing a
fold-preferring fallback is stale/wrong relative to the code sample that
follows it in the same brief; I did not implement that fallback path. No
other discrepancies found between brief prose and brief code.

## Test counts

- `tests/fleet/test_claim_expiry.py`: 11 passed
- `tests/fleet/test_claim_expiry_observe.py`: 6 passed
- Combined targeted run: 17 passed
- Full `tests/fleet/` suite (broader verification, run after both commits
  landed, per the "commit before long verification" rule): 1415 passed,
  0 failed, 14 warnings (all pre-existing pgpy deprecation/TODO warnings
  unrelated to this change)

```
$ python3 -m pytest tests/fleet/test_claim_expiry_observe.py tests/fleet/test_claim_expiry.py -q
tests/fleet/test_claim_expiry_observe.py ......                          [ 35%]
tests/fleet/test_claim_expiry.py ...........                             [100%]
============================== 17 passed in 0.49s ==============================

$ python3 -m pytest tests/fleet/ -q
====================== 1415 passed, 14 warnings in 41.93s ======================
```

## Mutation check

Target: the exclusive-boundary condition in `evaluate()`, called out in
the brief as the one place a naive implementation gets it wrong
(`test_exactly_at_ttl_is_not_reclaimable`).

Mutated `elif idle <= ttl_seconds:` to `elif idle < ttl_seconds:` (makes
exactly-at-ttl incorrectly reclaimable):

```
$ python3 -m pytest tests/fleet/test_claim_expiry.py -q
collected 11 items
tests/fleet/test_claim_expiry.py ..F........                             [100%]
=================================== FAILURES ===================================
____________________ test_exactly_at_ttl_is_not_reclaimable ____________________
tests/fleet/test_claim_expiry.py:37: in test_exactly_at_ttl_is_not_reclaimable
    assert v.reclaimable is False
E   AssertionError: assert True is False
E    +  where True = ExpiryVerdict(card_id='c1', owner='jarvis', claim_revision='r1',
       idle_seconds=172800.0, reclaimable=True, reason='idle-beyond-ttl').reclaimable
=========================== short test summary info ============================
FAILED tests/fleet/test_claim_expiry.py::test_exactly_at_ttl_is_not_reclaimable
========================= 1 failed, 10 passed in 0.60s ==========================
```

Restored `idle <= ttl_seconds`, reran full targeted suite, clean:

```
$ python3 -m pytest tests/fleet/test_claim_expiry_observe.py tests/fleet/test_claim_expiry.py -q
collected 17 items
tests/fleet/test_claim_expiry_observe.py ......                          [ 35%]
tests/fleet/test_claim_expiry.py ...........                             [100%]
============================== 17 passed in 0.43s ==============================
```

## Formatting / lint / dash check

- `black src/skcapstone/fleet/claim_expiry.py tests/fleet/` - no changes
  needed after each edit (both runs: "All done", files unchanged).
- `ruff check src/skcapstone/fleet/claim_expiry.py` - "All checks passed."
- `grep -n "—\|–" <files>` (em dash / en dash) - no matches in
  either the implementation file or either test file.

## Concerns / notes for the next task

- Neither `reap_dead_claims()` nor `_parse_worker_owner()` was touched;
  they were not present in `src/skcapstone/fleet/*.py` at all (they live
  in a deployed script referenced by `tests/test_skfleet_reaper_provenance.py`,
  which is outside this file and outside the scope of Tasks 1-2). Task 3
  or later presumably wires `observe`/`evaluate` into that path.
- `observe()`'s `last_owner_event_at` matching uses `writer == owner OR
  owner == owner` per event. This means any event where the OWNER field
  happens to equal the current owner (not just the claim event) also
  counts, which is intentional and matches the brief, but worth flagging:
  if a future event type reuses `owner` for something other than "who
  now holds the card" (e.g. a transfer-target field), this heuristic
  would need revisiting.
- Step 5 of the Task 2 brief ("Validate against the real store, read-only")
  was not performed: no copy of the live chiap01 CardStore was available
  in this worktree, and the brief explicitly says to skip it rather than
  write during this step if no copy exists. Real-store validation is
  deferred to deploy time as instructed.
