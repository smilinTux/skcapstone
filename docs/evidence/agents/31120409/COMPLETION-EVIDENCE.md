# Fiber gateway bridge source candidate

Card: `31120409`. Producer: `codex-gateway-bridge-31120409`.
Base: `5f621b47bfe98efe4bdee59282cb06ba5c20bf7b`.
Branch: `work/31120409-gateway-bridge`.
State: source checks passed; independent review and deployment are outstanding.

## Prerequisite and change

Commit `a0a0ebaaef17316835465dc0d10918d450cc9484` imports the exact reviewed
bridge and tests from `28d72706e0538cb06e62734c7ab6f1eb22e42833`.
The prerequisite bridge SHA256 was
`25115162e88ecd306ed010b195b5eb898b79edb0b57b54ce9c417277e370e228`,
matching the deployed usage-accounting bridge. Its five tests passed before
this repair. Usage totals, coverage, private receipt mode, no-clobber receipt
publication, and the legacy `local_budget_refusals` field remain intact. That
legacy field is now zero because this bridge no longer imposes a request cap.

The host bridge preserves gateway HTTP error statuses, discards upstream error
bodies and secret headers, and relays only normalized retry timing for 402,
429 and 503. The private bridge preserves that timing through the second hop.
Redirects remain refused. Neither hop retries requests, including uncertain
timeouts. The first 64 sanitized failure records remain a memory bound on
diagnostics, not a limit on requests.

The gateway determines request volume and output-token ceilings. Positive
integer budgets above 8192 and omitted budgets reach the gateway unchanged.
Malformed and nonpositive budgets, unexpected options, wrong routes/models,
oversized bodies, invalid claims and wrong response attribution remain refused.

The gateway HTTP idle timeout is now 660 seconds, covering the observed live
300-second queue and 300-second backend contract with retry/margin allowance.
The private hop waits 690 seconds. These are transport failure bounds, not
worker-count or token budgets. A future longer gateway contract requires
requalification of these transport values. A timed-out call is not replayed by
this bridge.

## Files changed

- `ops/fiber-f1be0929/fiber_gateway_bridge.py`
- `ops/fiber-f1be0929/tests/test_fiber_gateway_bridge.py`
- `ops/fiber-f1be0929/tests/test_fiber_sandbox.py`
- `changelog.d/31120409-gateway-bridge.md`
- This evidence file.

## Verification

Before the repair, four meaningful tests failed: gateway 429 became 502,
larger budgets were rejected, request 65 was refused, and a scaled real queue
wait exceeded the old timeout. The actual TCP-to-Unix-to-HTTP tests now pass.
They cover 66 successful requests, budgets of 16384 and 32768, gateway default
budgets, 402/429/503 and 401 errors, valid numeric/date Retry-After values,
malformed/injected values, redirected credentials, attribution failures, a
delayed gateway, and no duplicate call following uncertain timeout.

Commands used `/home/skuser01/.skenv/bin/python`:

```text
-m pytest -q ops/fiber-f1be0929/tests/test_fiber_gateway_bridge.py ops/fiber-f1be0929/tests/test_fiber_sandbox.py
22 passed in 8.07s

-m pytest -q ops/fiber-f1be0929/tests tests/test_fiber_lifecycle_finish.py tests/test_fiber_lifecycle_recovery.py tests/test_fiber_lifecycle_connection.py
351 passed, 3 skipped in 43.85s
```

The three existing conditional skips are two qualified-local-sandbox candidate
checks and one remote resource check. No new skip was introduced. An earlier
broad run had 349 passes and one obsolete sandbox expectation rejecting
999999 solely because of the removed token ceiling. That expectation now
tests nonpositive-budget rejection; the full command was rerun successfully.

`git diff --check` passed. No production API calls, protected inputs, secret
values, live worker launches, deployment, push or merge occurred in these
checks. Synthetic credentials are confined to local fixtures.

## Acceptance and rollback

Independent DeepSeek review of `7abd26505cc32aedbc9d12b7db692c32a10ff9c5`
returned FAIL. Its final demand to suppress valid `Retry-After: 0` contradicts
the gateway-owned retry policy and is not adopted. Its numeric-header finding
identified an unnecessary ten-character sub-bound: a valid leading-zero value
such as `0000000000001` was dropped. The follow-up removes that sub-bound while
retaining the 64-byte header boundary and adds an actual two-hop HTTP regression.
The original provider judgment is retained under the card's operator evidence.
The follow-up requires its own exact independent review before installation.
After the numeric-header correction, the focused bridge/sandbox command above
was rerun: **22 passed in 8.57s**. The broad 351-pass run applies to the prior
candidate; the follow-up changes only numeric retry-header normalization and
adds its real HTTP case to the same focused test.

Gateway status and retry timing survive both real bridge hops; error bodies
and credentials do not. Both duplicated throughput ceilings are removed while
usage accounting and authorization boundaries retain passing coverage.

Independent exact-candidate review and deployment verification remain required.
No native source card completion is claimed by this document. Deploy only
after preserving each host's exact current bridge bytes and mode; atomically
install the reviewed file and rerun bridge checks. Rollback restores that
preimage. The deployed accounting prerequisite must never be replaced with
the older bridge from the base revision. No data migration is required.
