# Card 33f64a43: unobserved worker terminal recovery

## Finding

On chiap08, reservation `87530478a94d1205d7335e1d16fce9614a27aa3fe5514b3c2506b65897f8714d` retained `intent.json`, `start.json` and `fenced-start-required.json` for `skfleet-worker-codex-070a85f6.service`, but no `observed.json`. `systemctl --user show` returned the exact unit as not-found/inactive/dead with no remaining process. The user journal contained exactly one `Started` and one `Consumed` event for invocation `aa28f2f344734312a3df85e3a60d4493`.

The installed occupancy path recovered this case only for builder units, so it continued charging 3 GiB. No production reservation, claim, or systemd unit was modified during diagnosis.

## Change

Admission now shares exact start and terminal journal recovery between builder units and lane worker units. A worker record is accepted only when its unit embeds the bound card ID, its owner and claim revision are valid, its immutable start record matches the reservation, the unit is no longer loaded, and a single invocation has exactly one start and one later terminal event. The normal observed and journal-terminal receipts are written with exclusive-create semantics. Missing, duplicate, mismatched or ambiguous evidence leaves the reservation charged.

## Validation

- Regression before implementation: `python -m pytest -q tests/fleet/test_unobserved_builder_terminal.py -k 'unobserved_worker'` produced 1 failed and 3 passed; the positive worker case remained charged.
- Focused suite: `PYTHONPATH=src python -m pytest -q tests/fleet/test_admission.py tests/fleet/test_admission_claim_fence.py tests/fleet/test_failed_admission_terminal.py tests/fleet/test_production_admission.py tests/fleet/test_successful_admission_terminal.py tests/fleet/test_unobserved_builder_terminal.py` -> 132 passed.
- Formatting: `black --check src/skcapstone/fleet/production_admission.py tests/fleet/test_unobserved_builder_terminal.py` -> passed.
- Lint: `ruff check src/skcapstone/fleet/production_admission.py tests/fleet/test_unobserved_builder_terminal.py` -> passed.
- `git diff --check` -> passed.

## Limits and rollback

This change proves and records only systemd-collected terminal invocations; it does not release claims or reservations based on age or unit absence alone. If evidence is missing, capacity remains charged for governed recovery. Roll back through a normal forward revert if deployed behavior is unsound; the immutable source reservation records remain available.
