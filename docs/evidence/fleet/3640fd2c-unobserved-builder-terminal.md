# Collected native builder admission recovery

Card: 3640fd2c. Operator: jarvis. Base: main 400af6892e4afed77ecf4bd620ecfd69ee9aaeed.

After governed #928 retirement of c60a542e, Niobe offered a new remote review to chiap03 at 23:49:56 UTC on 2026-10-04. The node claimed and released it on memory admission deferral without starting a worker.

Read-only host inspection found three 3 GiB reservations with intent.json and consumed start.json but no observed.json. All three units were collected, so the existing observed-invocation journal recovery did not run. Memory admission subtracted 9 GiB plus headroom from 14.07 GiB available, leaving 1.966 GiB versus the 3 GiB request.

Exact native unit journal start and terminal pairs:

| Unit request | Invocation | Start realtime microseconds | Terminal realtime microseconds |
|---|---|---|---|
| 9b5acdd3 / 077cd881cd572d8f212353c22c96e1a0910cd9484ea59f4c72d943fcea0456c9 / 1 | d967a1d940f04539acde687ed52e2332 | 1791095388806645 | 1791095638158127 |
| 9b5acdd3 / 81a177de1dd12085f12762a2f611ef52426f0bd7cab5158586954382d6ec13ba / 1 | 3d59c642e97d4f61b122a7796f56d33d | 1791096101539341 | 1791096195034763 |
| a0196e7c / 9cc319213d42a04c8c6650ac388a5542cf3aea039c37cb2c2e0eda8ef24fea65 / 1 | 8a3e35ab61fa4ec3b86803f6a880bdd8 | 1791121804744295 | 1791121923835317 |

The journal uses USER_UNIT and USER_INVOCATION_ID. Start MESSAGE_ID is 39f53479d3a045ac8e11786248231fbf; all three terminal records use ae8f7b866b0347b9af31fe1c80b127c0. No reservation was hand-deleted and no claim or service was changed.

The change reuses existing observed and terminal receipts. Before recovering an unobserved start it requires the exact native start receipt and unique skfleet-builder-CARD-REQUEST-ATTEMPT.service name. It queries only attributed manager start/terminal messages, with a five-second timeout and a 64 KiB result bound. Exactly one invocation and one start must exist, followed by a terminal event. Existing terminal validation rechecks the absent unit after journal inspection. Missing, malformed, ambiguous, live or reused evidence remains charged. Fixed worker unit names are excluded from this inference.

Normal reserve_launch calls recover this proof even without an optional node-admission cap. The consumed start, intent, source claim and candidate custody remain untouched; no worker command is replayed. A recovered observed.json plus journal-terminal.json carry both terminal and start entry hashes. Existing receipt handling owns retries. A partial receipt publication remains fail closed.

Validation commands and final counts are recorded in the linked PR. Tests cover successful recovery with and without strict accounting, source-claim preservation, admission of the next offer, safe receipt replay, and uncertainty cases. Rollback is a forward code revert; generated immutable receipts stay preserved. Deployment must use merged main, owned by lumina-nor.

Validation at the candidate:

```sh
python -m pytest -q tests/fleet/test_unobserved_builder_terminal.py tests/fleet/test_production_admission.py tests/fleet/test_remote_review_policy.py tests/fleet/test_admission_claim_fence.py tests/fleet/test_failed_admission_terminal.py tests/fleet/test_successful_admission_terminal.py tests/fleet/test_remote_review_dispatch.py tests/fleet/test_review_retire.py -m 'not host_systemd'
python -m black --check src/skcapstone/fleet/production_admission.py tests/fleet/test_unobserved_builder_terminal.py
python -m ruff check src/skcapstone/fleet/production_admission.py tests/fleet/test_unobserved_builder_terminal.py
python scripts/changelog_fragments.py --check
git diff --check
```

170 tests passed, 1 host-systemd test deselected; static, changelog and diff checks passed. Before implementation the new reproduction had 3 failures and 13 passes, including the two strict/non-strict accounting failures; no test was xfailed. Tests use synthetic native cards and stubbed journal/unit queries, with no production host connections.
