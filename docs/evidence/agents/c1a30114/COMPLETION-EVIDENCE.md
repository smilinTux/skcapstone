# c1a30114 source candidate evidence

Parent: `9b230773`. Owner: `codex-pr-association-c1a30114`. Claim: `be28eccf1c3b4f75bc313044b0ba37b6` remains held. This document records source readiness, not canonical card completion or installation approval.

## Purpose and changes

Add the narrow `coord archive-historical-pr` native operation for an exact `card_pr_repository_mismatch`. It guards the raw folded card, complete supported history, owner, claim, original PR, repository and transition under the existing mutation lock. A structured historical association is retained before clearing only the active PR link. The existing atomic event/idempotency and supported overlay primitives preserve all previous events, criteria, source head, verdict and outcome. The ordinary candidate snapshot guard is unchanged.

Changed source: `src/skcapstone/historical_pr.py`, `src/skcapstone/cli/coord_historical_pr.py`, and CLI registration in `src/skcapstone/cli/coord.py`. Added `tests/test_historical_pr.py`, changelog fragment, plan and validation evidence.

## Validation and applicability

Implementation candidate `7514198d44f7fcb7ef2a87e523127b2aee95534f`, tree `8247b1f11bb54b8b03b5b8569c2cae5874cd7352`, is based on `a5a8bafc79046e0f18b1ec4b7a819bfdfb612e28`. The original private descriptor is preserved with SHA256 `b6557dcb80c207297f6f350ab71972d65ea991750991fa68e42ab4a56099babe`.

The exact command and case coverage are in `VALIDATION.md`: **148 tests passed in 22.35 seconds** on Python 3.12.3, including 40 new cases. Ruff, Black checks and `git diff --check` passed. Coverage includes stale digests, claim and ownership changes, history-only changes, conflicting lineage/metadata, unrelated binding errors, denied authorization, interrupted writes and exact replay, while checking that outcomes and historical evidence remain unchanged.

This follow-up commit adds only this document. Source and test bytes remain identical to the tested candidate, so the 148 tests were not rerun. Its exact final commit/tree and eight file hashes are recorded in the new private descriptor.

## Pending gates and rollback

Independent provider review of the final exact candidate and root-controlled installation remain pending. No push, installation, live scanner-card repair, claim release, acceptance-criteria change or physical-device acceptance occurred. No live data migration has been performed.

Before any installation, root must reverify and back up the installed `cli/coord.py` preimage, SHA256 `77970a42e7b35dab6b548ede243fd6d940ea355b442767891e01877a713c5dd8`. The new `historical_pr.py` and `cli/coord_historical_pr.py` modules were absent at candidate preparation. Installation rollback restores that exact CLI preimage and removes only the exact newly installed module bytes after verifying their candidate hashes. Preserve the candidate, review receipts and any runtime evidence. Any later card reconciliation is append-only; rollback must never delete or rewrite its historical events.
