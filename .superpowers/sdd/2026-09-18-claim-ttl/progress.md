# SDD ledger, plan: docs/superpowers/plans/2026-09-18-claim-ttl.md

Spec: docs/superpowers/specs/2026-09-18-claim-ttl-design.md
Branch: feat/claim-ttl-heartbeat (worktree ~/skworld-worktrees/claim-ttl-skcapstone)
Base: e432e227 (origin/main, the deployed commit)

## Pre-flight conflict scan

| rows | finding |
|---|---|
| T1 vs T2 | Same file (claim_expiry.py). T2 appends `observe` + `import json`; T1 defines the dataclasses it returns. Sequential, dispatched to ONE agent to avoid two review cycles on one file. No conflict. |
| T2 vs T3 | T3 consumes observe/evaluate/ttl_seconds_from_env/mode_from_env. Signatures fixed in T1/T2 Interfaces blocks. Agree. |
| T3 vs T5 | T5 adds a console script pointing at claim_expiry_cli, created in T3. T5 MUST follow T3 or packaging names a nonexistent module. Ordered. |
| T4 vs all | Only task touching skfleet-rotate.py. Consumes T1-T3. Last. |
| T1 self | Tests assert boundary is exclusive (`idle <= ttl` is not reclaimable) and impl uses `idle <= ttl_seconds`. Agree. |
| T2 self | Tests assert re-claim supersedes (latest generation wins) and impl replays in seq order setting revision on each claim. Agree. |
| T3 self | Test asserts exit 0 with findings; impl returns 0 unconditionally. Agree. |
| T4 self | Plan gives structure not full code, by design: the helper must bind to names (HOME, log, d, HOST) that exist in a 6842-line script the plan cannot quote safely. Step 1 mandates reading them first. |

Ruling: T1+T2 batched to one implementer (same file, tightly coupled).
Cost if wrong: one larger review surface instead of two small ones.

## Tasks

### Task 1+2: complete
Commits: 4011dd06 (decision module), e8d3224e (observe), 3e4a8e4b (report)
Tests: 17 targeted pass; tests/fleet/ 1415 pass 0 fail. Verified independently by controller.

Ruling: my Task 2 brief's Step 3 PROSE ("prefer the real fold, import
CardStore, fall back to local replay") contradicted its own code sample
(pure direct event-log replay, no CardStore). The implementer followed the
code sample and flagged the contradiction. The code sample is correct and
the prose was wrong: `observe` needs the last event the OWNER wrote, which
the fold does not retain, so a fold-based implementation would have to read
the event log anyway and the CardStore import would buy nothing while
adding a hard dependency on skcoord being importable.
Cost if wrong: none identified. The fold remains the authority for
OWNERSHIP, and the replay reproduces it (claim sets, release clears, in seq
order), which is why the census agreed with the fold at 349.

Ruling: Task 2 Step 5 (validate against a copy of the real store) deferred
to deploy time, as the brief permits. Controller validated equivalently
against the LIVE store read-only instead: the same replay logic reported
349 held claims, matching CardStore.fold() exactly, and after the sweep
reported 159, matching the 190 releases the CLI reported.

### Controller finding after Task 2: the plan's sort key was wrong
Commit: 2170b89b

Validating observe() against the LIVE chi store (the step the brief allowed
deferring) found the replay disagreeing with CardStore.fold(): 98 cards the
replay called held, the fold called free.

Cause, and it was mine: the plan specified
`events.sort(key=(seq, ts))`. Events are sharded ONE FILE PER WRITER
(`jarvis@chiap03.jsonl`, `pi-codex-chiap01-<cid>@chiap01.jsonl`,
`coord@chiap08.jsonl`) and `seq` restarts at 0 in every file. Sorting by
seq first interleaves shards and can place a release before a later claim
by a different writer, so the replay ends with the owner still set.

Card bf80259a, the worked example:
  pi-codex-chiap01-bf80259a@chiap01  seq=0 claim   06:17:51
  pi-codex-chiap01-bf80259a@chiap01  seq=1 claim   06:18:40
  jarvis@chiap03                     seq=0 release 06:27:06

Fixed to `(ts, seq)`. Regression test encodes the three-shard shape and is
mutation-checked (restoring seq-first fails that test alone).

LESSON for the remaining tasks: no unit test built from a single synthetic
event file could have caught this. The shape only exists in the real store.
Task 4 must re-validate against the live store before enforce mode is ever
enabled, not just pass unit tests.

### Task 3: complete
Commits: 431cb7c9 (feat), 13389050 (fix), b662db3f (docs)
Tests: 6 targeted pass; tests/fleet/ 1422 pass 0 fail.
The implementer hit `test_any_other_skcapstone_path_is_a_named_exemption`
(the same relocation guard that Task 1 of the seat-lease work tripped) via
the CLI's `--home ~/.skcapstone` default, and resolved it by adding a
justified exemption entry rather than suppressing the check. Correct call.

### Task 5: complete (done by controller, not dispatched)
Commit: 886a0ebe
A two-line pyproject change plus its test did not warrant a subagent.

### OUT OF SCOPE FINDING, fixed: the fleet was not dispatching at all
Commit: 1b1bd9bf

While verifying that swept cards get picked back up, found that ZERO of 190
released cards were re-claimed across four rotate cycles. The dispatcher
was healthy and running every 5 minutes, and ending every cycle in NOOP:

    LANE_ADMISSION_BLOCKED|chiap01|<card>|codex=unknown,escalate=unknown,
      glm=unknown,kimi=unknown,qwen=unknown
    SELECTION_EMPTY|chiap01|reason=no-compatible-lane pool=24 free=5
    NOOP|chiap01|selection empty

Measured: 373 consecutive NOOPs, ZERO launches in 3 days.

Cause: all three rotate hosts had
SKFLEET_GATEWAY_URL=http://<host>:18790/v1. The probe appends "/health"
and "/queue" to that base, and those live at the gateway ROOT:

    /v1/health -> 404      /health -> 200
    /v1/queue  -> 404      /queue  -> 200

Both probes raised HTTPError, every lane resolved to `unknown`, and
`lane_health()` is fail-closed on unknown by design. chiap08's niobe-live
and seraph units carried the correct origin form, which identified the
intended value. Commit e1ada0e7 (Sep 12) had removed the hardcoded
`http://chiap01:18790` default and made the variable mandatory.

Fixed live on chiap01/02/03, then hardened in code (`gateway_root()`
discards any path), doc corrected, and a `_strict_opener` test added
because the existing mock resolves a request by its final path segment
only and therefore could never distinguish `/v1/health` from `/health`.

Result: lane states went 13 unknown -> 7 healthy, and 5 workers launched
across chiap01 and chiap02 within two minutes. Sustained since.

LESSON, worth carrying into the TTL rollout: the claim backlog was a
SYMPTOM. Releasing 334 claims returned them to the pool, and the pool grew
from 24 to 29 as expected, but nothing would ever have picked them up.
A claim-TTL mechanism enabled while dispatch was dead would have looked
like it was working (claims released on schedule) while delivering nothing.
This is why the spec's phase 2 gates on evidence from the fleet rather than
on elapsed time.

### Phase-2 baseline, measured on chi 2026-09-18 after the sweep and the
### gateway fix, with dispatch healthy again

22 held claims, and the owner-idle distribution is starkly bimodal:

    7 claims   idle 0.0h to 0.2h     all column.doing, freshly launched
    0 claims   idle 0.2h to 31.5h    nothing at all in this band
   15 claims   idle 31.5h to 518.1h  abandoned

A 150x gap separates live work from abandoned work. This is the empirical
case for the design: "has the owner touched the card" discriminates without
ambiguity, and it needs no worker cooperation, no new event type, and no
heartbeat (which is dead fleet-wide anyway).

At the chosen 48h TTL, exactly 2 of the 15 reclaim immediately (513.8h and
518.1h); the remaining 13 sit at 31h to 45h and cross the line over the
following 3 to 17 hours. The measurement also shows a 24h TTL would be safe
here (100x margin to the nearest live claim), but 48h stays the shipped
default because the failure is asymmetric and nothing is lost by waiting.

The two oldest are the two the manual sweep could not take:
  72df1b66  no claim_revision, so no CAS fence exists  (518.1h)
  cec6b1c0  owner "pi", failed the CAS check in batch 2 (513.8h)
Both are exactly what an expiry path should collect, since neither can be
resolved by proving a process absent.

USE THIS TABLE as the phase-2 comparison set: when report mode runs, its
would-reclaim list should contain the stale rows and none of the 7 active
ones. An active card appearing in it is a phase-1 defect and blocks enforce.

### Controller findings pending the final review (not yet fixed)

Two items found by reading Task 4's output, held rather than edited so the
reviewer is not working against a moving file:

**(a) Default-off is not deploy-safe.** `_expire_idle_claims()` imports
`skcapstone.fleet.claim_expiry` at the TOP of the function, above the
`mode == "off"` early return, and is called unguarded at module scope
(skfleet-rotate.py:4474) between `reap_dead_claims()` and the
review-and-close phases. A host carrying the new script with the old
package therefore raises ImportError even in default-off mode, reaps, and
then never reaches `open_provisional_reviews` or `close_reviewed_parents`.
Today the only protection is deploy ordering (package first, then script),
which is the exact trap that already bit this repo once via the
`GATED_EXIT_CODE` import. Fix: move the mode check above the import so off
mode touches nothing at all. Severity: should-fix before deploy.

**(b) The replay does not apply the fold's CAS fence on release.**
`observe()` clears ownership on ANY `release_claim`, while the fold accepts
one only when `released_owner` equals the current owner AND
`expected_claim_revision` equals the current revision. A rejected release
therefore leaves the fold holding the card while the replay reports it
free. The divergence direction is the safe one (the replay under-reports,
so it never releases something the fold thinks is held), but it means such
a card is invisible to the very mechanism meant to collect it, which is the
same failure mode as the `assign` gap. Chi currently has no such event,
which is why the 22 = 22 comparison is clean; this is latent, not live.
Severity: should-fix, with a regression test for a mismatched-revision
release.
