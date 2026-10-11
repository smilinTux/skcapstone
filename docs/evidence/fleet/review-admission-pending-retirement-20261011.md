# Unacknowledged review admission retirement

Base: origin/main at 12a9d8d64992dbb4f86cc842194ebd1943f86f1e.
Branch: fix/review-admission-pending-retirement.

The authority recovery sweep previously sent admission-pending statuses to
retire(), which requires an acknowledged running generation. retire_prestart()
refused every existing status. The nine reported cards were read through coord
show and their dispatch statuses inspected: all had released claims, review
columns, admission-pending statuses, and empty execution.invocation fields.
No production retirement or other board mutation was performed for this change.

The optional status_sha256 argument selects a narrowly bound admission-pending
mode in retire_prestart(). It preserves authority, request, offer, card revision,
criteria, source, hold, exit-custody, session, admission inventory and exact
release checks. The pending status must bind the same request, reviewer, claim,
node, host, unit and reservation. A launch with launched=true is refused, as is
an ambiguous launch record. An explicitly unsuccessful launch is allowed.
The release actor may differ from the recovery actor, matching the existing
running-generation retirement contract; the owner and claim must match exactly.

A destination systemctl query uses the existing strict SSH options. A missing,
inactive or failed reserved unit must have zero main and control PIDs. Unknown
states, active units, timeouts, unavailable hosts and matching tmux sessions
refuse. No guessed invocation is supplied to the running-generation death check.
A matching unobserved reservation may remain, but changed intents, invalid start
records or additional custody receipts refuse. Reservation files are preserved.

The request, status and prestart proof are archived before the native retirement
event and pointer removal. retired_offers() checks the archived status hash and
pending binding before recognizing the retirement. The existing authority-only,
non-dry recovery call in skfleet-rotate.py invokes the new path every cycle;
no new timer, service, policy switch or scheduler is introduced. This path does
not release claims. Refusals preserve pointers and are reported by the sweep.

## Validation

Initial TDD run before implementation:

```text
python -m pytest tests/fleet/test_review_admission_retire.py -q
18 failed: retire_prestart() did not accept status_sha256
```

Final run:

```text
python -m pytest tests/fleet/test_review_retire.py tests/fleet/test_review_admission_retire.py tests/fleet/test_review_generation_recovery.py tests/fleet/test_remote_review_dispatch.py tests/fleet/test_remote_review_execution.py tests/fleet/test_remote_review_policy.py -q
159 passed in 58.47s
```

The 38 new tests cover dry-run and application, receipt recognition and tampering,
failed/inactive/collected units, strict remote routing, invocation and launch
refusals, host/unit/request/claim identity, active ownership and stale claim
metadata, missing/duplicate releases, source/criteria/hold changes, review packet
and acknowledgment custody, exit receipts, malformed admission data, reservation
start evidence, unreachable hosts, timeouts, sessions, pointer races, and two
successive sweep cycles without a worker-exit receipt.

```text
python -m ruff check src/skcapstone/fleet/review_retire.py src/skcapstone/fleet/review_pending.py tests/fleet/test_review_admission_retire.py
All checks passed!
python -m black --check src/skcapstone/fleet/review_retire.py src/skcapstone/fleet/review_pending.py tests/fleet/test_review_admission_retire.py
3 files would be left unchanged.
git diff --check
clean
```

All retirement writes and host calls in the new tests use temporary storage and
mocked service boundaries. No deploy, running-service change, production sweep
execution or feature enablement was performed. PR 1129 separately addresses the
failed unit name on future launches. Reverting this change restores the prior
refusal of all prestart status files; existing receipts remain preserved.
