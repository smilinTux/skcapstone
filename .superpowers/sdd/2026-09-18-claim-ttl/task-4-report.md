# Task 4 report: the second release path in the reaper

Status: **complete**. Commit `c81661f4` on `feat/claim-ttl-heartbeat`
(`2faf13be` was the same work before the test stubs were sharpened; it was
amended, not replaced by a second commit).

    scripts/fleet/skfleet-rotate.py         | 145 +++++++++++
    tests/fleet/test_claim_expiry_reaper.py | 410 ++++++++++++++++++++++++++
    2 files changed, 555 insertions(+)

## What was added

Three module-level helpers between `reap_dead_claims()` and the `if DRY:`
guard, plus one call site:

- `_claim_ttl_release_cmd(card_id, owner, revision)` builds the release
  invocation. It is the same argv shape the absence path uses, including the
  `--expected-claim-revision` CAS fence, so there is exactly one way this
  script releases a claim.
- `_claim_ttl_fresh_state(cid)` pops `_rows` and `_claim_rows` for that card
  and returns `lifecycle_state(cid)`. Both caches, because `lifecycle_state`
  reads through `_authoritative_card_snapshot` -> `_strict_card_events`,
  which memoizes into `_claim_rows`, and the absence path's own
  `_rows.pop()` does not clear that one. Both release paths run in one tick,
  and this one acts on observations taken before the other ran, so a cached
  fold can say "claimed" about a card released seconds earlier.
- `_expire_idle_claims(observations=None, runner=None, state=None, now=None,
  env=None, dry=None)` is the decision. Every parameter after
  `observations` exists only so the path can be driven from a test without
  executing this script's module scope; production calls it with no
  arguments.

The call is `_expire_idle_claims()` appended to the non-dry branch of the
existing `if DRY:` guard, beside `reap_dead_claims()` rather than inside it.

### Why beside the function and not at its end

The brief allowed either. Beside it is strictly better on two counts:

1. **`reap_dead_claims()` stays byte-identical**, which is the strongest
   available form of success criterion 5. There is nothing to reason about.
2. `reap_dead_claims()` returns early twice before its loop, on quorum
   shortage and on known-host visibility loss. A call at the end of that
   function would inherit both early returns, so the new path would be
   disabled exactly when cross-host visibility is degraded. An absolute
   idle deadline needs no host to prove absence of anything, so making it
   depend on quorum would smuggle back in the predicate the design removes.

## Proof the existing loop is unmodified

Only additions, no deletions:

    $ git diff --stat origin/main -- scripts/fleet/skfleet-rotate.py
     scripts/fleet/skfleet-rotate.py | 145 ++++++++++++++++++++++++++++++++++++++++
     1 file changed, 145 insertions(+)

    $ git diff origin/main -- scripts/fleet/skfleet-rotate.py | grep -c '^-[^-]'
    0

And the function body itself, extracted by `ast` and compared as text
against `origin/main`:

    reap_dead_claims identical to origin/main: True

The regression suites for that path agree: `tests/test_skfleet_reaper_provenance.py`
(55) and `tests/test_reaper_stall.py` (2) pass, 57 total, the same 57 that
passed before the change.

`tests/fleet/test_claim_expiry_reaper.py::test_reap_dead_claims_is_untouched_by_this_change`
pins it going forward: nothing inside that function may name the new helpers
or `claim_expiry`.

## The three constraints that define this path

Enforced by test, not by comment:

- **No `_parse_worker_owner()`.** That gate recognizes only
  `pi-<lane>-<host>-<cid>` shaped owners and skips every other owner before
  any liveness logic runs, which accounts for 146 of the 349 held claims
  (`jarvis` 104, `codex` 28, `seraph` 5). `test_a_bare_session_owner_is_reclaimable`
  drives `jarvis`, `codex`, `seraph` and `codex-w72-backend-r3` through the
  path and asserts all four release (success criterion 4).
- **No cross-host authority.** `test_the_expiry_path_depends_on_no_absence_proof`
  is parametrized over `_parse_worker_owner`, `live_report`, `REAP_QUORUM`
  and `CLAIM_GRACE` and asserts none of them appears in the unparsed source
  of the three helpers. It also asserts all three helpers were found, so it
  cannot pass vacuously against an empty string.
- **Default off.** `mode_from_env` returns `off` unless
  `SKFLEET_CLAIM_TTL_MODE` is set, and `off` returns before `observe()` runs
  and before anything is logged. `report` logs `CLAIM_TTL_WOULD_RECLAIM` per
  candidate and releases nothing. `enforce` additionally refuses on the
  script's `DRY` flag (`"--go" not in sys.argv`), and in practice is also
  behind the existing `if DRY:` guard at the call site, so there are two
  independent reasons a dry run releases nothing.

## Never trusting the exit code

Each release is bracketed by two fold re-reads:

- **Before**: if the card is no longer `claimed`, log `CLAIM_TTL_SKIPPED`
  and skip. This is what stops the two paths from both releasing one
  generation, since the absence path runs first in the same tick.
- **After**: if the card is still `claimed` despite a zero exit, log
  `CLAIM_TTL_INEFFECTIVE` and do not count it. A zero exit is not proof the
  claim moved: when the two stores disagree the CLI answers "Already
  released" and writes nothing, which was confirmed live on card `f17d9e32`.

A non-zero exit logs `CLAIM_TTL_FAILED` and continues to the next card. That
is the healthy outcome when a worker re-claimed since the observation: the
CAS fence answers with a refusal, never a theft.

One deliberate omission: the new path does **not** write to the absence
path's `_record_ineffective` suppression store. That store feeds the
absence path's own gate, and cross-writing it would couple two paths whose
independence is the whole point. The cost is that a persistently
ineffective release retries on each tick while `enforce` is on; the log line
names the divergence so it is visible, and enforcement is a deliberate human
switch rather than a default.

## Tests

`tests/fleet/test_claim_expiry_reaper.py`, 20 tests, all passing. The
helpers are lifted out of the shipped script with `ast` and exec'd against a
stubbed namespace, the way `tests/test_reaper_stall.py` already does, so the
tests exercise the source that runs on the fleet. Importing the script is
not an option: it acquires the rotation lock and drives a full pass at module
scope.

    tests/fleet/test_claim_expiry_reaper.py       20 passed
    tests/test_skfleet_reaper_provenance.py       55 passed  (unchanged)
    tests/test_reaper_stall.py                     2 passed  (unchanged)
    tests/fleet/ (full suite)                   1444 passed in 42.58s

`black` and `ruff` are clean on the new test file. `ruff` was not run as a
fixer on the script, which has 118 pre-existing findings; instead its
findings were compared before and after, by rule and count:

    IDENTICAL ruff findings before and after

The one finding the first draft did add was `I001` on the function-local
import, fixed by splitting it one name per line. No long typographic dashes
anywhere: `grep -nP '[\x{2013}\x{2014}]'` over both files returns nothing.

## Mutation check

Dropping the post-release fold re-read, so the path trusts the release
return code:

    -        if state(v.card_id) == "claimed":
    +        if False:
                 log(d, "CLAIM_TTL_INEFFECTIVE|...

    =================================== FAILURES ===================================
    ________________ test_a_zero_exit_is_not_proof_the_claim_moved _________________
    tests/fleet/test_claim_expiry_reaper.py:303: in test_a_zero_exit_is_not_proof_the_claim_moved
        assert released == 0, "a zero exit code counted as a reclaim"
    E   AssertionError: a zero exit code counted as a reclaim
    E   assert 1 == 0
    =========================== short test summary info ============================
    FAILED tests/fleet/test_claim_expiry_reaper.py::test_a_zero_exit_is_not_proof_the_claim_moved
    ========================= 1 failed, 19 passed in 1.81s =========================

Two more mutations were checked and reverted, both caught:

- dropping the **pre**-release check (`if state(v.card_id) != "claimed"` ->
  `if False`): 5 failed, 15 passed, including
  `test_enforce_skips_a_card_the_absence_path_already_released`.
- routing through the owner-shape gate (replacing the revision guard with
  `if not _parse_worker_owner(v.owner, v.card_id, None): continue`): 8
  failed, 12 passed, including
  `test_the_expiry_path_depends_on_no_absence_proof[_parse_worker_owner]`.

The state stubs were rewritten after the first mutation run, which exposed
them as call-order sensitive: a flat `iter([...])` sequence made an unrelated
test fail on how many times the helper happened to read the fold.
`_claimed_then_open()` now answers per card, so a failure means something
real.

## Live read-only check, this host

Report mode driven against the real store on `noroc2027` (mode `report`,
`DRY` true, a runner that would raise if called):

    released: 0 elapsed: 0.9s
    CLAIM_TTL_WOULD_RECLAIM|noroc2027|0e010500|pi-codex-noroc2027-0e010500|revision=9e8c333f55a147f793c48104ccc1730c idle_h=66.8
    CLAIM_TTL|noroc2027|mode=report candidates=1 released=0

That is the same single verdict the Task 3 report CLI produces on this host,
from the same `observe()` and `evaluate()`:

    RECLAIM 0e010500  idle=   66.8h  pi-codex-noroc2027-0e010500        idle-beyond-ttl

Two independent readers, one answer. `observe()` over the whole store costs
under a second, so `report` mode is affordable on a five-minute cycle.

## Concerns for Task 5 and the rollout

1. **Nothing enables this yet, by design.** `SKFLEET_CLAIM_TTL_MODE` is
   unset on every host, so the deploy is a no-op until a human sets
   `report`, and the phase-2 comparison table in `progress.md` is the gate
   on `enforce`.
2. **`no-claim-revision` is a permanent hold, not a delay.** On this host 14
   of 15 held claims have no `claim_revision`, so no CAS fence exists and
   this path will never release them. They need the separately scoped
   one-time sweep or a repair, and no amount of waiting will clear them.
   Worth stating plainly in the rollout notes so the report list is not read
   as "these will resolve themselves".
3. **Three hosts will each act on the same store.** Two hosts that both
   decide to reclaim one card will both call `release-claim`; the CAS fence
   makes the loser a `CLAIM_TTL_FAILED` line rather than a double release,
   and the pre-release fold re-read usually avoids the call entirely. Under
   Syncthing lag of 10 to 20 seconds, expect occasional `CLAIM_TTL_FAILED`
   and `CLAIM_TTL_INEFFECTIVE` lines on enforce; they are the fence working,
   not a defect.
4. **`--abandon-reason error`** is copied verbatim from the absence path so
   the two releases share one invocation shape. The spec asks for
   `reason: "lease-expired"` to make the action distinguishable in the
   event log, which the current CLI vocabulary does not offer. The
   distinguishing signal today is the `CLAIM_TTL_RECLAIMED` evidence line,
   not the event. If auditability of the event itself matters, that is a
   CLI change and belongs in its own card.
