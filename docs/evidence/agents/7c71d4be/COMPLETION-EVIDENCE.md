# Source candidate: 7c71d4be

Parent 9b230773. Base b7c0bea695729a7ea8a455d7eafada929fff4c5a is the
qualified authority source, including its guarded CLI and evidence-commit
review instructions. Native prerequisite 80f2c3a6 is exact SKCoord candidate
772de7212a803446a7f84806d11ff54fdbfce7ab.

The new coord review-replace command validates a private operator authorization
and exact source, claim, candidate, raw predecessor fold/history, attributable
invalidation, retained report/decision, launch receipt and terminal process.
Its --check mode changes no card. An actual invocation appends one typed native
authorization on the invalid predecessor and resumes the existing opener. It
never launches, completes, releases, changes source, or erases old CI history.

The review identity gains only a validated authorization digest; default IDs
are unchanged. Identity functions are re-exported from link_review_work for
compatibility and extracted to keep that existing module below 500 lines.
The index includes the attempt. Exact replay and lost acknowledgement resume
the same card, including an already claimed replacement without relaunching it.

current_review_attempt(home, source_card, head) is the strict initial acceptance
boundary for Feature A. The narrower snapshot predecessor exclusion validates
historical authorization and unchanged invalid predecessor, allowing legitimate
later source PASS/DONE reads without granting completion authority. It never
suppresses arbitrary malformed PR links. Later predecessor writes invalidate
the exception. Existing source-only applicability validation is unchanged.

Validation command:

```text
PYTHONPATH=src:/home/skuser01/work/skcoord-80f2c3a6-review-replacement/src python -m pytest tests/test_review_replacement.py tests/test_guarded_review_work.py tests/test_link_review_work.py tests/test_review_verdict.py tests/test_seraph_review_publisher.py tests/fleet/test_seat_runtime.py -q --disable-warnings
```

Result: 239 passed, zero skipped, 5.28 seconds. Log:
~/.skcapstone/evidence/work/7c71d4be/TESTS-FINAL.log. A prior command named a
nonexistent test path and ran zero tests; TESTS-PATH-REFUSAL.log retains that
refusal. The successful command above names the actual fleet test file.
Changed runtime/new tests pass Ruff and Black; git diff --check passes.

Runtime files: review_replacement.py, review_work_identity.py,
guarded_review_work.py, link_review_work.py, seraph_review_cardstore.py and
cli/coord_review_work.py. The top-level cli/coord.py is unchanged. Installation
must include the exact two-file SKCoord prerequisite and verify each authority
preimage; rollback restores those preimages and removes newly installed modules.

Limitations: source-only candidate, not installed. Root prepares the real
operator authorization after qualification. Collected units require both
retained launch and terminal evidence and fresh no-unit/no-PID checks; missing
or reused process evidence refuses. External deterministic review/test
acceptance is a separate prerequisite. This does not revive Fiber or accept
source89508f83 or invalid review3054d5f1. Independent review receipts are retained
outside this immutable source candidate. No push or live mutation occurred.

Real predecessor qualification exposed a legacy invalidation link without
event_id. Both validators now bind its complete event_sha256. The application
fixture uses the actual native CLI and asserts this legacy event shape; a
wrong digest refuses. The complete suite passed again: 240 passed in 6.91
seconds, zero skipped, in TESTS-LEGACY-DELTA.log. Read-only validation of the
actual pending operator document passed without writing authorization or
opening review work. Prior exact reviews remain retained; only this small
compatibility delta receives an additional independent review.
