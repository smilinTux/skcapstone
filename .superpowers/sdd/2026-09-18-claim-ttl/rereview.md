# Scoped re-review: the two review blockers

Reviewer: adversarial re-review, 2026-09-18. Scope is commit `cf30375b`
("fix(fleet): address both review blockers") against base `6cf6aa7b`, which is
exactly the tree the prior review examined. Nothing else on the branch is
re-judged here.

Everything marked CONFIRMED was produced by running code: the shipped test
suite, the shipped `observe()` against the live `~/.skcapstone` store on
noroc2027 read-only (5861 cards), or purpose-built stores under the session
scratchpad driven through the real `CardStore`. Scratch removed.

## Test run, done myself

    python3 -m pytest tests/fleet/ tests/test_fleet_lane_health.py -q
    1498 passed, 14 warnings in 46.20s   (exit 0)

Matches the commit message's claim of 1498. CONFIRMED. The three named new
tests were run individually and all PASS, none skipped.

**That green depends on an uncommitted edit.** See NEW-0: the source half of
the default-off fix is not in the commit, and the committed test for it fails
against the committed tree.

---

# NEW-0. BLOCKER (confirmed). The default-off deploy-safety fix is NOT COMMITTED. The committed test for it FAILS against the committed tree.

`git diff --stat 6cf6aa7b HEAD` lists 11 files and
`scripts/fleet/skfleet-rotate.py` is not among them:

    git show --stat --oneline HEAD | grep -c skfleet-rotate   ->  0

`git status --porcelain` in the worktree:

    M scripts/fleet/skfleet-rotate.py
    git diff --stat  ->  1 file changed, 16 insertions(+), 2 deletions(-)

That one hunk IS the fix. The committed `HEAD:scripts/fleet/skfleet-rotate.py`
still has the lazy import ABOVE the mode check, which is precisely the defect
the commit message claims to have closed:

    # HEAD:scripts/fleet/skfleet-rotate.py, _expire_idle_claims
    """
    from skcapstone.fleet.claim_expiry import (
        evaluate, mode_from_env, observe, ttl_seconds_from_env,
    )

    env = os.environ if env is None else env
    mode = mode_from_env(env)
    if mode == "off":
        return 0

The TEST is committed (`tests/fleet/test_claim_expiry_reaper.py:412`), the
CODE is not. So the suite is green only because of a working-tree edit that a
`git pull`, `git checkout` or `git reset` erases, and because
`tests/.../test_claim_expiry_reaper.py:35` reads the script from the worktree
path rather than from git.

Proved it rather than reasoning about it. Extracted the committed script to a
temp file, pointed the committed test module's `SRC` at it, and ran the
committed test unmodified:

    git show HEAD:scripts/fleet/skfleet-rotate.py > head-rotate.py
    monkeypatch.setattr(M, "SRC", HEAD_SRC)
    M.test_off_mode_returns_without_importing_the_package(tmp_path, monkeypatch)

    FAILED
    tests/fleet/test_claim_expiry_reaper.py:442: in test_off_mode_...
        assert expire(env={}) == 0, "off mode must not import anything"
    head-rotate.py:4382: in _expire_idle_claims
        from skcapstone.fleet.claim_expiry import (
    E   ImportError: simulated half-deployed host: no skcapstone.fleet.claim_expiry

Failure scenario, and it is the exact one the commit message describes: merge
this branch as committed, deploy `skfleet-rotate.py` to a host whose
`skcapstone` package is a release behind, and set nothing. `off` mode reaches
the import, raises `ImportError`, and because
`_expire_idle_claims()` is called unguarded at `skfleet-rotate.py:4488`
between `reap_dead_claims()` and the review-and-close phases, the host reaps
claims and then aborts the rest of every cycle. Default-off stops meaning
"touches nothing". A second, immediate consequence: CI on the merged branch
goes red on `test_off_mode_returns_without_importing_the_package`.

This is also a repo hard-rule violation (CLAUDE.md: "Before you call any task
'done', commit the code that backs it"; "Commit BEFORE the long verification").
The commit message asserts the fix as landed, which is the failure mode the
rule exists to catch: a report that says "done" over an unsaved worktree.

**Fix: `git add scripts/fleet/skfleet-rotate.py` and amend `cf30375b`.** The
edit itself is correct; I reviewed it in place (see the default-off section
below) and everything I verified there holds for the working-tree version. It
just is not in the branch.

CONFIRMED.

---

# Blocker verdicts

## B1 (gateway endpoint mismatch): **RESOLVED** for lane admission.

The fix is `fleet_lane_health.py:337`:

    if snapshot.get("endpoint") != gateway_root(endpoint):

**Is normalizing inside `lane_health()` sufficient for every consumer of the
snapshot's `endpoint` field?** Yes. Grepped `src/` and `scripts/` for
`get("endpoint")` and `["endpoint"]`: the only comparison of that field
anywhere is `fleet_lane_health.py:337`. `operatorapp.py:100` reads an
unrelated operator-manifest field. Nothing else consumes
`snapshot["endpoint"]` at all, so there is no second site left seeing raw
versus normalized. CONFIRMED by grep.

**Is there a caller that legitimately needs the path preserved and now
silently loses it?** No. `lane_health()` uses `endpoint` for nothing but that
one comparison, so normalizing the argument cannot strip a path anyone
downstream wanted. The two callers that genuinely need the operator's `/v1`
value still get the raw constant: `skfleet-rotate.py:1426`
(`env["SKFLEET_GATEWAY_URL"]=_GATEWAY_ENDPOINT`, handed to workers) and
`skfleet-rotate.py:6722` (`resolve_and_preflight`, which builds
`base + "/v1/chat/completions"`). Neither was touched. CONFIRMED by reading.

**Does `active_gateway_revision` still work?** Yes.
`fleet_lane_health.py:77-112` reads only `parsed.hostname` and `parsed.port`,
so a discarded path is irrelevant. It is now called with the NORMALIZED value
from inside `acquire_lane_snapshot` (`:271`) and with the RAW value from
`skfleet-rotate.py:1422`; both produce the same host and port. CONFIRMED by
reading.

**Does the revision check have the same class of bug?** No, and for a
structural reason: at `skfleet-rotate.py:6106` the caller computes
`_active_gateway_revision = str(_lane_health_snapshot.get("runtime_revision")
or "")` and then passes that same value back in as `active_revision`
(`:6113`). The comparison at `fleet_lane_health.py:338-340` is therefore a
self-comparison at the only production call site, so raw-versus-normalized
cannot arise. (The `REVISION_RE.fullmatch` half still bites on an empty
revision. That the equality half is tautological here is pre-existing on the
branch, not introduced by this fix, and out of scope.) CONFIRMED by reading.

**Test quality.** `test_a_v1_base_url_is_admissible_end_to_end_not_just_sealed`
(`tests/test_fleet_lane_health.py:491`) now runs the `/v1` base through
`acquire_lane_snapshot` with a strict opener that 404s `/v1/*`, then calls
`lane_health()` with the RAW `/v1` value and asserts `(True, "healthy")`. That
is the assertion whose absence let the half-fix look complete. It would fail
against the old `endpoint.rstrip("/")`. `test_the_root_form_is_still_admissible`
pins the non-regression. Both genuinely bite.

### NEW-1. SHOULD-FIX, confirmed. The corrected doc sentence is still false: the `/v1` form is not tolerated end to end, it zeroes the codex lane.

`docs/fleet/lane-admission-health.md:81-84` now asserts `gateway_root()`
normalizes "in BOTH places that matter ... so the `/v1` form is tolerated end
to end." There is a third place, and it is not normalized.

`src/skcapstone/fleet/review_capacity.py:53` -
`acquire_review_route_snapshot()` does `endpoint = base_url.rstrip("/")` and
then fetches `endpoint + "/v1/models"`, `endpoint + "/health"` and
`endpoint + "/queue"` (`:56-58`). It is called from
`skfleet-rotate.py:1395-1399` with the RAW `_GATEWAY_ENDPOINT`. With the
`/v1` form set, all three requests are wrong (`/v1/health`, `/v1/queue`,
`/v1/v1/models`), the whole `try` raises, and the snapshot is sealed as
`{"routes": [], "error": ...}`.

Confirmed consequence, run:

    eligible_gateway_routes({... 'routes': [], 'error': 'HTTPError'}, 'S', [], {})  ->  []
    aggregate_review_capacity([], 8)                                               ->  0

`skfleet-rotate.py:1550-1554` assigns that 0 to `_codex["free"]`, so the codex
lane gets zero free slots and dispatches nothing. Failure scenario: an
operator reads the doc sentence, leaves `SKFLEET_GATEWAY_URL=http://chiap01:18790/v1`
in place, sees every lane admitted healthy, and still gets a total codex
dispatch outage with no `endpoint-mismatch` and no probe errors to point at.
This is the same root cause as B1 in a path the fix did not reach. It is
pre-existing code, but the doc claim asserting otherwise is NEW in this
commit. Either fix `review_capacity.py:53` to `gateway_root(base_url)` for the
health and queue fetches (it needs the root for two of three URLs and the root
plus `/v1` for the third), or narrow the doc sentence to lane admission only.
CONFIRMED (by running `eligible_gateway_routes`/`aggregate_review_capacity`,
and by reading the call chain).

---

## B2 (`observe()` diverged from the fold): **RESOLVED.**

The hand-rolled replay is gone. `claim_expiry.py:206-207`:

    from skcoord.card_store import CardStore
    cards = CardStore(root).list_cards(include_archived=False, degrade_unreadable=True)

Ownership comes from `card.owner`, the fence from `card.meta["_claim_revision"]`,
and the event log is read only by `_last_owner_event_at()` for a timestamp.
Divergence is no longer possible by construction: the answer IS the fold's.

Live-store verification on noroc2027 (read-only; `CardStore.__init__` calls
`validate_shared_home`, which only expands the path, and `observe()` performs
no writes, both confirmed by reading and by the `test_never_writes_anything_under_home`
mtime check):

    card ids                                  5861
    list_cards(include_archived=False)        1744
    fold held, including archived              143
    fold held, non-archived                     95
    observe() reported                          95   <- exact agreement
    unreadable-degraded cards                    0

    verdicts at the 48h default: no-claim-revision 56, idle-beyond-ttl 39

Compare the prior review's numbers for the same store: fold-held 143,
replay-held 106, 37 missed, 38 with a wrong revision. The 75-card divergence
is 0. CONFIRMED.

### Is taking `meta["_claim_revision"]` correct and complete?

The fold's writes to that key are at `card_store.py:1479` (from
`core["initial_claim_revision"]`, and `:1476-1478` RAISES if an owner has no
revision, so that seed can never be owner-without-fence) and `:1599`
(`claim`, where `:1578` is `revision = e.get("claim_revision") or
e.get("event_id")`). It is popped at `:1504` (`void`), `:1516` (`assign`),
`:1519` (`unassign`), `:1554` (accepted `release_claim`), `:1601` (a `claim`
whose revision resolves to neither a claim_revision nor an event_id) and
`:1609` (`complete`). Of those, only `assign` sets an owner while popping the
fence.

So the states where the fold has an owner and no `_claim_revision` are exactly
two: the documented `assign` case, and a `claim` event carrying neither
`claim_revision` nor `event_id`. Measured on the live store:

    56 held non-archived cards have no _claim_revision
    last owner-setting action among those 56:  Counter({'assign': 56})
    claim events with neither claim_revision nor event_id:  0

CONFIRMED: `assign` is the only such state in practice, and the fenceless
`claim` shape does not occur because `append_event` always writes an
`event_id`. The commit message's "documented, not fixed" note is therefore
accurate in kind. It is badly understated in degree, which is NEW-2 below.

F2a is genuinely fixed: the prior review's worked example `0a3c64b8` (a claim
with `claim_revision: null` and `event_id: f89302e5...`) now appears in the
reclaim list WITH a fence. CONFIRMED.

### What does `degrade_unreadable=True` actually do?

`card_store.py:1766-1780`: per card, `fold()` inside a `try/except
Exception`; on failure it logs `logger.error` and substitutes
`_unreadable_card()` (`:1734-1754`), a placeholder with `status=BACKLOG`,
`labels=["unreadable"]`, `meta={"unreadable": True, ...}` and **no owner**.

- **Can it fabricate a held card?** No. The placeholder has no owner, so
  `observe()`'s `if not owner: continue` (`claim_expiry.py:211-212`) drops it.
  CONFIRMED by reading and by the corrupt-line probe below.
- **Can it silently hide one?** Yes. Ran it: a real store with one claimed
  card, then one malformed JSON line appended to its shard.

      observe(root) before -> [ClaimObservation(card_id='aaaaaaaa', owner='w1', ...)]
      observe(root) after  -> []

  The held card vanishes from the report with nothing in the rotate log (only
  a `logger.error` on the `skcoord` logger, which the dispatcher does not
  surface). Direction is safe (under-report, never a bad release) and the live
  store has 0 unreadable cards today. This is the deliberate trade for not
  letting one corrupt card blank the whole report, and it is the right trade,
  but it is untested: the prior `test_observe_survives_a_corrupt_line` was
  deleted and nothing replaced it. SHOULD-FIX (a test plus a count in the
  report line). CONFIRMED.

### Is `include_archived=False` the right filter, and is `archived` the only terminal signal?

`include_archived=False` is right and it fixes F2b. `void` (`:1502-1509`) sets
`archived=True` AND clears the owner, so a voided card is doubly excluded.
`archive` sets `archived=True` and clears nothing, so it is the case that
matters; ran it:

    B fold: owner='w2' archived=True rev='r2'
    B in observe(): False

So the 48 archived-and-owned cards on the live store (143 held minus 95) are
now excluded, which matches `lifecycle_state()` returning `"void"` for
anything archived. Report and actor now agree on that population, where the
prior code reported 37 of them and the reaper would have skipped every one.
CONFIRMED.

**`archived` is NOT the only terminal signal.** A card can be `status=done`
with an owner. `complete` (`:1606-1609`) clears the owner, but a bare `move`
to the done column does not. Ran it:

    C fold: status=done owner='w3' archived=False
    C observe: ClaimObservation(card_id='ccccccc1', owner='w3', claim_revision='r3', ...)
    C verdict: ('idle-beyond-ttl', True)

That row is reported as RECLAIM and then permanently skipped by the reaper,
because `lifecycle_state()` (`skfleet-rotate.py:2403-2415`) returns
`"claimed"` only for `{ready, doing, review}`. This is the prior review's F5,
explicitly deferred as a non-blocking SHOULD-FIX, and the fix did not touch
it. Its live magnitude is now measured: of the 39 reclaimable rows, 10 have
status `backlog` and `lifecycle_state()` would refuse all 10.

    reclaimable status dist: ready 14, backlog 10, doing 8, review 7
    reclaimable rows lifecycle_state would NOT call "claimed": 10

26% of the phase-2 would-reclaim list is unactionable by construction. Better
than the prior 16-of-106 figure, still present. CONFIRMED.

### Is the `owner`-field match in `_last_owner_event_at` right?

`claim_expiry.py:165`:

    if event.get("writer") != owner and event.get("owner") != owner:

The `released_owner` worry does not materialize: a `release_claim` by a third
party names the freed identity in `released_owner`, not `owner`, so it cannot
match. Measured which actions carry an `owner` field at all on the live
store's held cards: `claim` 849, `assign` 56, nothing else. So the exposure is
exactly a third party writing a `claim` or `assign` that names the owner.

That exposure is large and it is the load-bearing one for idleness:

    held cards whose FRESHEST "owner activity" event was written by SOMEONE ELSE:  49 of 95
    actions supplying that foreign freshness: Counter({'assign': 49})
    held cards where the owner is NEVER a writer:  41 of 95
      e.g. ('0cc03099', 'legacy-export', writers ['lumina','reconcile'])
           ('3446a8a9', 'lumina',        writers ['opus','reconcile'])

All 49 are today `assign`-derived, and `assign` pops the fence, so all 49 are
already refused as `no-claim-revision`. The dangerous variant is a foreign
`claim`, because that one KEEPS a fence. Built and ran it:

    A before third-party touch: rev='r1'  idle=500h   (reclaimable)
    A after  a third party appends {"action":"claim","writer":"mero","owner":"w1","claim_revision":"r1"}:
      owner='w1' rev='r1' idle=-0.00h  ->  reason='future-timestamp', reclaimable=False

The fold accepts it (`card.owner == owner`, so the conflict branch at
`:1581-1586` does not fire), so the claim stays fenced and the idle clock
resets to now. A reconciler or proxy writer that re-records claims on behalf
of workers keeps every dead claim alive indefinitely. The prior review noted
this class and rated it "Acceptable" because the direction only ever
under-reclaims, which holds. It is recorded here as NEW-3 because the new code
inherits it verbatim while the measured exposure (49 of 95) is far larger than
the prior review's "one soft spot" framing, and because
`test_another_actors_events_do_not_refresh_the_owners_idleness`
(`tests/fleet/test_claim_expiry_observe.py:198`) only exercises the `writer`
half: it appends `mero_observation` events with NO `owner` field, so the
`owner`-field branch is untested. Safe-direction, confirmed.

### Does it read only `*.jsonl`? Yes, and that is now correct.

`claim_expiry.py:150-151` skips anything whose suffix is not `.jsonl`. The
fold does the same (`card_store.py:1380`), so F2g's `.jsonl.tmp` divergence is
gone. CONFIRMED by reading both.

One residual: `_last_owner_event_at` walks `root / "cards" / card_id /
"events"` with plain `Path` calls, not `CardStore`'s `O_NOFOLLOW` descriptors,
so it would follow a symlinked shard the fold refuses. Reachable only for a
card the fold already folded successfully, direction is under-reclaim, and
there are no non-`.jsonl` entries or symlinks in the live store. THEORETICAL,
NIT.

### Performance

Measured on the live store, 5861 cards, 95 held:

    list_card_ids   0.15s
    list_cards      1.96s
    observe total   1.99s

Inside a 300s cycle that is 0.7%. `list_cards` folds all 5861 (reading every
card's shards once), then `_last_owner_event_at` re-reads only the 95 held
cards' shards, so the double read is bounded by the held set, not the card
count. `_legacy_cache` is per-instance and `observe()` builds exactly one
`CardStore`, so legacy events are loaded once. Pathological case is a single
card with a very large event log, which costs the same in `list_cards`
regardless; nothing here is quadratic. ACCEPTABLE. CONFIRMED.

### Does the new hard dependency on `skcoord.card_store` create a failure mode?

`skcoord>=0.1.77` is a declared dependency (`pyproject.toml:59`), so an
`ImportError` requires a half-deployed host, not a missing extra. In `off`
mode nothing is imported at all (see the default-off section). So no NEW
ImportError surface in the default configuration.

But going through the fold DOES add an uncaught-exception surface in `report`
and `enforce`, which is NEW-4 below.

---

# New defects

## NEW-1. SHOULD-FIX (confirmed). `docs/fleet/lane-admission-health.md:81-84` claims the `/v1` form is tolerated end to end; `review_capacity.py:53` is a third unnormalized consumer and it zeroes codex capacity.

Detailed above under B1. Confirmed by running
`eligible_gateway_routes`/`aggregate_review_capacity` and by reading
`skfleet-rotate.py:1395` and `:1550-1554`.

## NEW-2. SHOULD-FIX (confirmed). The "three cards on chi" note understates the unreclaimable population by 18x; on this store 59% of held claims can never be collected.

`src/skcapstone/fleet/claim_expiry.py` (the `assign` case) and the commit
message's closing note: "Three on chi are in that state and need `coord
unassign`."

On noroc2027, 56 of 95 held non-archived cards are in that state, all 56 with
`assign` as their last owner-setting action, and they are not idle backlog
cruft: their status distribution is review 29, ready 22, doing 5. So the first
`report`-mode run on this host prints 56 rows the mechanism will never act on
against 39 it would, and the operator reading "the TTL is working, those are
just fenceless" is reading a majority of the problem population. The prior
review made exactly this argument about F2a and it applies unchanged to the
`assign` half.

This is a documentation and reporting defect, not a correctness one: the
refusal itself is right, because releasing an assignment needs `coord
unassign` and there is no revision to fence on. What is wrong is the recorded
magnitude and the absence of any signal in the report that distinguishes
"nothing to do" from "56 cards need a different tool". Fix: correct the note
to a per-store count rather than a chi count, and have
`skfleet-claim-expiry` and the `CLAIM_TTL` report line emit the
`no-claim-revision` count alongside the candidate count.

## NEW-3. SHOULD-FIX (confirmed, safe direction). A third party can renew a fenced claim forever via `claim_expiry.py:165`, and the new test does not cover that branch.

Detailed above. Failure scenario: `reconcile` (which already writes
owner-naming events to 49 of 95 held cards on this host) starts re-recording
`claim` rather than `assign`. Every such card then reports idle ~0 on every
cycle and is never reclaimed, and the operator sees a clean `candidates=0`
that looks like success. Under-reclaims only, so it cannot steal live work.
`tests/fleet/test_claim_expiry_observe.py:198` tests only the `writer` half.

## NEW-4. SHOULD-FIX (confirmed). Two uncaught exceptions from `list_card_ids()` now abort the dispatcher cycle mid-way, in `report` and `enforce` mode only.

`_expire_idle_claims()` is called unguarded at
`scripts/fleet/skfleet-rotate.py:4488`, between `reap_dead_claims()` and the
open-provisional-reviews / close-reviewed-parents phases. `list_cards`'s
`try/except Exception` (`card_store.py:1768-1774`) wraps only `fold()`;
`list_card_ids()` at `:1767` is OUTSIDE it and raises
`ValueError("CardStore card entry is unsafe")` on a symlinked entry in
`cards/` (`:1713-1714`) and whatever `validate_card_lock_identifier` raises on
a bad name (`:1717`).

Ran both against a real store:

    symlinked card entry:  RAISED ValueError CardStore card entry is unsafe
    stray bad-named dir:   tolerated (skipped by the name filter)

So one symlink under `~/.skcapstone/cards/` aborts the rest of the cycle after
the absence path has already released claims. `~/.skcapstone` is a single
Syncthing folder shared across the fleet, which is precisely where a stray
symlink arrives from. The old replay would have silently followed it.

This is the same trap the commit message describes for the lazy import, and
the mode-before-import reorder does not cover it: the reorder protects `off`,
which is the mode that never calls `observe()` anyway. Fix: wrap the
`observe()` call (or the whole body after the mode gate) in a `try/except
Exception` that logs `CLAIM_TTL_FAILED` and returns 0, so this path can never
take the cycle down. Not reachable while the mode is `off`.

## NEW-5. NIT (confirmed). Dead code in a test makes it look stronger than it is.

`tests/fleet/test_claim_expiry_observe.py:86-92` does

    [got[cid]._replace(last_owner_event_at=time.time() - 100 * HOUR)]
      if hasattr(got[cid], "_replace") else [got[cid]]

`ClaimObservation` is a frozen `@dataclass`, not a `NamedTuple`, so it has no
`_replace`; verified: `hasattr(o, "_replace") -> False`. The branch never
runs and the observation is evaluated fresh, so the aged path the author
intended is not exercised. The surviving assertion (`reason !=
"no-claim-revision"`) still bites, because an absent fence produces that
reason regardless of freshness, so the test is not vacuous. Replace the
`hasattr` with `dataclasses.replace`.

## NEW-6. NIT (theoretical). Duplicated mode parsing cannot disagree today, but nothing pins that.

`skfleet-rotate.py:4392-4395` re-implements the parse as
`str(env.get(...)).strip().lower()` and accepts only `("report", "enforce")`;
`claim_expiry.mode_from_env` uses the identical expression against
`_MODES = ("off", "report", "enforce")`. Enumerated the disagreement space: a
value in `{report, enforce}` passes both; `off` and every other value
(including a non-string, since both `str()` it) returns 0 from the first and
`"off"` from the second. No input can disagree. The hazard is future: adding a
fourth active mode to `_MODES` would be silently ignored by the hardcoded
tuple. A one-line test asserting the two agree across a token list would pin
it. (The `if env is not None else ""` at `:4394` is dead: the preceding line
already replaced `None` with `os.environ`.)

---

# The smaller fixes

## `ttl_seconds_from_env` floor and non-finite guard: CORRECT AND COMPLETE.

`claim_expiry.py:78-84`. Exercised every path myself:

    ''        -> 48h      '0'      -> 48h     '-1'   -> 48h    '-0.0'  -> 48h
    '0.0001'  -> 48h      '.5'     -> 48h     '0.999999' -> 48h
    'nan'     -> 48h      'NaN'    -> 48h     ' -nan' -> 48h
    'inf'     -> 48h      '-inf'   -> 48h     'Inf'  -> 48h    'infinity' -> 48h
    '1e400'   -> 48h      '1e-400' -> 48h
    '48h'     -> 48h      'abc'    -> 48h     '0x10' -> 48h    '1,5'  -> 48h
    '\t\n'    -> 48h      missing  -> 48h     None   -> 48h
    '1'       -> 1h       '2.5'    -> 2.5h    '  72  ' -> 72h  '+2'   -> 2h
    '1e3'     -> 1000h    integer 24 -> 24h

Every rejection lands on the conservative 48h default and none raises, which
is right for something read inside a dispatcher cycle. `nan` is fixed:
`math.isfinite` catches it before the comparison that used to let it through.
The floor is exact-inclusive (`hours < MIN_TTL_HOURS`), so `'1'` is accepted
and `'0.999999'` is not. Two harmless curios: Python's float accepts
underscores (`'1_0' -> 10h`) and full-width digits (`'１２' -> 12h`).

**Does the floor break a legitimate short-TTL use?** No. Grepped every
`SKFLEET_CLAIM_TTL_H` in `tests/`: the only values used are `"24"`
(`test_claim_expiry_reaper.py:237`), `"nan"`, `"6"`, `"1"` and the
deliberately-refused sub-hour tokens. Every test that needs a short deadline
passes `ttl_seconds=` straight into `evaluate()` and bypasses the reader
entirely, which is the right seam. CONFIRMED.

## Default-off deploy safety: the EDIT is correct, but it is UNCOMMITTED (NEW-0).

Everything in this section describes the WORKING-TREE version of
`scripts/fleet/skfleet-rotate.py`, which is not in any commit. Against the
committed tree, none of it is true and the test below fails. See NEW-0.

`skfleet-rotate.py:4384-4405`. Between function entry and the `return 0` there
are exactly three statements: the `env` default, the `mode` parse, and the
membership test. None imports anything, and `str`/`.strip`/`.lower` are
builtins. The lazy `from skcapstone.fleet.claim_expiry import ...` is below the
gate. CONFIRMED by reading.

The new test (`test_claim_expiry_reaper.py:412`) is not vacuous. It
monkeypatches `builtins.__import__` to raise on any name containing
`claim_expiry`, asserts `off`, unset and `banana` all return 0, and then
proves the stub is live with `pytest.raises(ImportError)` on
`report`. `from X import Y` always routes through `builtins.__import__` even
when `X` is already in `sys.modules`, so the stub is not defeated by earlier
tests having imported the module, and the final assertion is what demonstrates
that. Ran it individually: PASSED. CONFIRMED.

The duplicated parse cannot disagree with `mode_from_env` for any input
(enumerated above). NEW-6 is the maintenance nit only.

## Tests rebuilt against a real `CardStore`

`tests/fleet/test_claim_expiry_observe.py` is now 247 lines against 588, and
every fixture goes through `CardStore.create` / `append_event`. The strong
ones are `test_a_release_with_the_wrong_revision_does_not_free_the_card`
(F2c), `test_a_second_claim_by_a_different_owner_does_not_steal_the_card`
(F2d), and `test_a_voided_card_is_not_reported`, which writes the historical
post-void claim straight to the shard because `append_event` now refuses it,
and so tests the READ path rather than the write guard. That one is
well-judged.

Weak or missing:

- `test_observe_agrees_with_the_fold_over_a_mixed_population` (`:220`) is
  near-tautological now, since `observe()` is a thin wrapper over the exact
  dict comprehension the test builds. It still has value as a pin against
  reintroducing a replay, but it does not independently verify anything. NIT.
- `test_another_actors_events_do_not_refresh_the_owners_idleness` covers only
  the `writer` branch (NEW-3).
- No test covers `degrade_unreadable` (the corrupt-line drop) or the `archive`
  exclusion, both of which I had to verify by hand.
- NEW-5, the dead `_replace` branch.

### The `_claimed` backdating helper: SOUND, for a reason worth writing down.

`tests/fleet/test_claim_expiry_cli.py:27-51` creates a card, appends a real
`claim`, then rewrites `ts` on disk for every event whose `writer` or `owner`
is the owner.

**Does it produce the state it claims?** Yes. Replicated the same technique
against a real store and confirmed `observe()` reports the backdated stamp
(`idle=500h`) and `evaluate()` returns `idle-beyond-ttl`.

**Does it leave the fold's view consistent?** Yes, and not by accident:

- The hash chain survives. `_read_events` (`card_store.py:1415-1421`) checks
  each line's `prev_hash` against the hash of the PRECEDING line, so editing
  line N would break line N+1. In `_claimed` the rewritten `claim` is the last
  line of the owner's shard, so there is no N+1. Inspected the shard: the
  claim event carries no `prev_hash` at all in this `skcoord` version
  (`[('claim', 'w1', False)]`), so the check does not even engage.
- Ordering survives. `fold()` re-sorts events only when `_legacy_events` are
  present (`:1481-1484`); `_read_events` otherwise preserves filename-then-line
  order, so the backdated claim keeps its position and still follows the
  create.
- The failure mode is loud, not silent. A broken chain would make `fold()`
  raise, `degrade_unreadable=True` would substitute an ownerless placeholder,
  `observe()` would drop the card, and the tests' `rows[stale]` lookups would
  `KeyError`. So the fixture cannot quietly stop testing what it claims.

One fidelity note, not a defect: the rewrite backdates the claim below the
card's own `created_at` in `core.json`, a state the real store could never
produce. Nothing under test reads `created_at`, so it does not matter, but if
the helper is ever reused for something that does, that is the trap.

---

# Merge verdict

**NO, not as it stands. One commit away.**

Both blockers themselves are RESOLVED, with evidence rather than assertion:

- **B1 RESOLVED.** The endpoint comparison normalizes at the single site that
  performs it, and there is no second comparison anywhere in `src/` or
  `scripts/` to disagree with it. `active_gateway_revision` is unaffected (it
  reads only host and port), and the revision check cannot carry the same bug
  because the only production call site feeds the snapshot's own
  `runtime_revision` back in. The new test reaches `(True, "healthy")` through
  `lane_health()` with the raw `/v1` value, which is the assertion whose
  absence let the half-fix pass.
- **B2 RESOLVED.** `observe()` reported 95 held cards against the fold's 95 on
  the same live store that produced the 75-card divergence, because it no
  longer holds an independent opinion to diverge with. The `nan` inversion and
  the sub-hour deadline are closed on every input I could construct.

**The blocker is NEW-0: the default-off deploy-safety fix is not in the
commit.** `scripts/fleet/skfleet-rotate.py` is an uncommitted working-tree
edit, the committed tree still has the import above the mode check, and the
committed test written to prove otherwise FAILS against it, which I ran. So
the 1498-green is a property of this worktree, not of the branch. Merging as
committed ships a red CI and a host that reaps and then aborts its cycle on
`ImportError` with the mechanism switched off, which is the one thing
default-off is supposed to make impossible.

That is a `git add` plus `git commit --amend`, and it is the whole of what
stands between this branch and a mergeable default-off deploy. Re-run
`python3 -m pytest tests/fleet/ tests/test_fleet_lane_health.py -q` after the
amend and confirm from a clean `git status --porcelain` rather than from the
worktree.

**With NEW-0 committed: MERGE for a default-off deploy.** None of NEW-1
through NEW-6 is reachable while `SKFLEET_CLAIM_TTL_MODE` is unset. NEW-1 is a
live-immediately doc defect over a live-immediately code cause, but the code
cause is pre-existing and the operator-visible harm needs the `/v1` form,
which the commit message states was already corrected on chiap01/02/03.

**Before `SKFLEET_CLAIM_TTL_MODE=report` is set anywhere:**

1. **NEW-4.** Wrap the `observe()` call at `skfleet-rotate.py:4488` so a
   `ValueError` from `list_card_ids()` cannot abort the cycle after the absence
   path has already mutated the board. This is the one new defect that can do
   damage beyond the mechanism's own reporting, and it is the same shape as
   NEW-0 in a path the mode gate does not cover.
2. **NEW-2.** Correct the "three cards on chi" note and put the
   `no-claim-revision` count in the report line. The report is the phase-2
   gate and it currently prints 56 unactionable rows against 39 actionable
   ones with nothing to tell them apart.
3. **F5, carried forward unfixed.** 10 of the 39 reclaimable rows have a
   status `lifecycle_state()` will refuse, so the report and the actor
   disagree by 26% by construction. The prior review deferred this and the fix
   did not address it; it is now measured.

**Before `enforce`:** NEW-3, the F5 reconciliation, and the prior review's F5
liveness annotation, which remains the only thing that makes success criterion
6 checkable.

NEW-1 should be fixed, or the doc sentence narrowed to lane admission,
whenever the branch is next touched, independently of the TTL rollout.
