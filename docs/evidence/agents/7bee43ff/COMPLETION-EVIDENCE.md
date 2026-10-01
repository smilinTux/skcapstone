# Authoritative execution link before candidate collection

Card: `7bee43ff`
Producer: `codex-execution-link-7bee43ff`
Base: `cd8570bb72bf0df5103569a8d7399485af4fcb43`

The rollout observed a terminal worker's local `execution_record` link reaching
the authority after the candidate revision had already been sealed. Removing
only that link from the later authoritative snapshot exactly reconstructs the
earlier review-request revision. The retained diagnosis is
`bd9a559a/DELAYED-EXECUTION-LINK-PROOF-1790830424711377130.json` in operator evidence.

## Change

`fiber_control.collect_candidate` validates the request using the existing
worker validator, performs the existing terminal candidate export, and then
reads the card freshly from the authority. It returns the unchanged proof only
when `links.execution_record` exactly equals
`STATE/controls/<validated token>/execution.json`. Missing or mismatched links
raise `candidate-execution-link-not-authoritative`, allowing a later controller
cycle to retry after replication. The same function covers producer and
reviewer collection.

No native writes, cached card reads, ignored revision fields, altered proof
validation, acceptance bypass, or worker-side behavior are introduced. Existing
claim, terminal invocation, candidate integrity, and strict lifecycle revision
checks remain in place.

Files changed:

- `ops/fiber-f1be0929/fiber_control.py`
- `ops/fiber-f1be0929/tests/test_fiber_execution_link.py`
- This evidence file.

## Verification

The new five-method regression suite on the unchanged base returned exit 1,
with 12 failing subcases: absent, null, wrong-token and wrong-file links for
both stages; delayed authority visibility; invalid tokens; and unavailable
authority reads. The existing remote-refusal behavior passed. After the fix,
all five methods pass, including delayed first read followed by exact second
read, unchanged returned proof identity, export-before-read ordering, and no
native writes.

Focused boundary command:

```text
PYTHONPATH=src:ops/fiber-f1be0929 python -B -m pytest -q -ra
  ops/fiber-f1be0929/tests/test_fiber_execution_link.py
  ops/fiber-f1be0929/tests/test_fiber_control.py
  ops/fiber-f1be0929/tests/test_fiber_candidate.py
  tests/test_fiber_lifecycle_connection.py
  tests/test_fiber_lifecycle_finish.py
  tests/test_fiber_lifecycle_recovery.py
```

Result: **126 passed, 2 existing conditional skips, exit 0, 23.03 seconds**.
The two skipped cases require the separately qualified live namespace harness.
Lifecycle source-race, supersession, recovery, and guarded finish checks pass.
`git diff --check` also passes.

Operator logs under `evidence/work/7bee43ff/`:

- `before-tests.log`: original failing regression.
- `boundary-tests.log`: passing focused boundaries, SHA256
  `1d2a931f7c3d791f47b6774434a6d0ed3fca3aa26ea5d842e9cc23dffcf2ac6f`.

## Limits and rollback

This is prevention for new candidate exports. It does not repair an already
sealed lifecycle intent or alter card `41a6039b`; the root operator owns that
separate recovery. The expected path follows the existing shared worker home
and STATE layout; a different layout refuses until explicitly qualified.

Independent exact-candidate review and root-controlled installation remain
pending. No deployed file, queue, timer, claim of another worker, or original
trial evidence was changed. Rollback is the prior exact `fiber_control.py`
preimage on the authority; no data migration is required. The external
candidate descriptor records the final commit, tree, and evidence hash.
