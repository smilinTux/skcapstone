# Production seat policy reconciliation

Card: abf059ff. Owner: codex-production-seats-abf059ff.
Claim: 4ee5fcc9b1824083849faca69427a7c2.

The native card base remains 92cdb4d7ad87ffb7eba897ebaae30ff5a1e1db97.
This candidate is composed over the authorized shared policy, independent
review and dispatcher dependencies, ending at local dependency commit
930e3262b9cf8d2d17912988511297e3227a8640. The leaf changes only the seat
entrypoint, its tests, this evidence, and the changelog.

Production Atlas and Seraph validate the existing dispatcher capability marker
and shared production policy before execution. They use policy models without
injecting fixed batch targets or legacy aliases. Seraph checks the exact model,
skgateway transport and one allowed backend domain in the typed native launch
receipt. Both seats preserve their existing exact claim and live-unit checks;
Seraph also preserves source-head, independent recommendation and duplicate
generation checks. Legacy behavior without a production policy is retained.

Atlas launch receipts do not contain a backend identity. Its independent
verification therefore proves policy lane/model, exact native claim, seat and
live unit, while the dispatcher separately verifies the backend preflight.
This candidate does not claim to add a backend field to Atlas receipts.

Shared dispatch budgets are 125 seconds for Atlas and Seraph. Their wrappers
allow 150 seconds and reap the entire process group on timeout. Together with
the existing 270-second Niobe wrapper, serial wrappers fit within 570 seconds
of the existing 600-second generation budget. No worker-count ceiling is added.

## Validation

Before the fix, new production regressions produced 10 failures and 8 passes:
both legacy batch refusals, five rejected Atlas policy routes, and three
accepted invalid Seraph provider/domain/host receipts. After the fix:

```text
python -m pytest tests/test_seat_cycle_entrypoint.py tests/test_seraph_dispatcher_path.py tests/test_seat_cycle_orchestrator.py tests/test_seat_cycle_orchestrator_budget.py tests/test_seat_cycle_recovery.py tests/test_niobe_live_entrypoint.py tests/test_rotation_lock_fairness.py tests/fleet/test_production_dispatch.py tests/test_production_policy.py -q
204 passed, 0 failed, 0 skipped
```

The interpreter was /home/skuser01/.skenv/bin/python. The test log is retained
outside the source tree under the card's private evidence directory. Tests
cover all four model families, both qualified Qwen domains, mismatched model,
provider, backend and authority host, stale claims, duplicate receipts,
unqualified dispatcher and configuration refusal, and an actual subprocess
whose child process is terminated with the timeout process group.

Ruff on the changed Python files and git diff --check passed.

## Acceptance and rollback boundary

No runtime files, node specs, services, timers, models or existing claims were
changed by this source leaf. Independent exact-candidate review and the
parent's composed deployment are still required. Revert only this leaf commit
to remove its behavior while preserving the separately reviewed dependencies.
