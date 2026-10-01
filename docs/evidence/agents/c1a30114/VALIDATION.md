# Source candidate validation

Card `c1a30114`, parent `9b230773`, owner `codex-pr-association-c1a30114`, claim `be28eccf1c3b4f75bc313044b0ba37b6`.

The existing guarded link requires a valid candidate snapshot. A historical PR whose forge differs from the active repository prevents that snapshot from being read. This candidate adds `coord archive-historical-pr` for that specific mismatch, without changing the ordinary guard.

The operation requires the exact raw folded card digest, complete supported native/overlay/archive history digest, owner, claim, previous PR, repository and transition token. It preserves a structured `historical_pr_association` link before clearing only the active `pr` link. Both events use the existing native mutation lock, atomic append/idempotency primitive and supported overlay projection. The owner, claim, acceptance criteria, source head, verdict and all previous history remain intact.

Replays reconstruct and verify the original card and history. Partial interruption leaves the mismatched PR active until lineage is durable. An intervening claim, outcome, link or history change rejects further work. Creation metadata PRs, conflicting bindings, invalid repositories and non-mismatched PRs are outside this operation.

Validation on Python 3.12.3:

```text
PYTHONPATH=src python -m pytest -q tests/test_historical_pr.py tests/test_guarded_coord_link.py tests/test_guarded_completion.py tests/test_review_replacement.py tests/test_guarded_review_work.py tests/test_guarded_candidate_verdict.py tests/test_guarded_source_completion.py
148 passed in 22.35s
ruff check: PASS for both new modules, coordination CLI and new tests
black --check: PASS for all four Python files
git diff --check: PASS
```

The 40 new cases cover successful archival, exact replay, interruption after either native append or overlay projection, partial interruption followed by changed custody, stale digests, history-only changes, claims, owners, lineage, outcomes, bindings, request changes, denied CLI authorization, malformed or absent repositories and conflicting creation metadata. Existing guarded link, completion, review and candidate verdict tests remain passing.

An initial test run identified that normalized legacy overlay history discards event IDs. The final guard hashes the original supported overlay projection rather than that lossy normalization. One test command named a nonexistent test file and collected zero tests; the corrected full command above passed. Early test assertions were corrected before this final run.

This is local source evidence only. No runtime installation, real card repair, source push, acceptance-criteria change or physical-device acceptance occurred. Independent review and root installation authorization remain required. Install rollback uses exact module preimages; any later card reconciliation remains append-only and must not erase historical events. No worker claim is released here.
