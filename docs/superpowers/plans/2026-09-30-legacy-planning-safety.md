# Legacy Planning Safety Implementation Plan

> Execute inline with regression tests, exact-source review and rollback. The referenced executing-plans skill is not installed.

**Goal:** Preserve the existing rotation while preventing planning from actuating workers.

**Architecture:** Keep the existing lifecycle and dispatch code. Apply its existing `DRY` switch to the two uncovered entrypoints, before invoking either function.

**Tech Stack:** Python 3.12, pytest, existing AST-based dispatcher tests.

**Spec:** User-approved minimal scheduler integration; native card `005ec3ec` under `3ee76566`.

## Global Constraints

- Preserve workers, claims, workspaces and old timer definitions.
- No new dependencies, automatic merge or application deployment.
- Legacy launch timers stay disabled pending unified admission qualification.
- Local-only snapshot commit is authorized for independent review.

## Task 1: Close the two planning actuation paths

Files: `scripts/fleet/skfleet-rotate.py`, `tests/fleet/test_rotation_dry_safety.py`, `tests/test_worker_liveness_runtime.py`.

- [ ] Add AST-isolated tests that execute the actual top-level watchdog and builder-offer blocks with recording fakes. Assert zero calls when `DRY=True` and one call when `DRY=False`.
- [ ] Run `pytest -q tests/fleet/test_rotation_dry_safety.py` and verify both dry cases fail before implementation.
- [ ] Guard `run_production_cycle(agent=_dispatch_writer())` with `if not DRY:`. Change the offer block to `if not DRY and _is_niobe_builder_host(HOST):`.
- [ ] Update the existing production-entrypoint structural assertion to accept the guarded invocation and require the guard.
- [ ] Run the new tests plus worker-liveness, wedge-reaper, builder-dispatch and dispatcher-writer regression tests.
- [ ] Snapshot locally, independently review the exact diff through the governed GLM lane, and externally verify reviewer evidence.
- [ ] Install only these exact substitutions into matching deployed dispatcher copies, preserving hash-pinned backups. Do not copy the whole source snapshot over newer deployed versions.
- [ ] Run the same isolated call tests against deployed bytes and record rollback and remaining integration gates on the native card.

This prerequisite is not a claim that the complete unattended W7 workflow has been restored. Planning may still write diagnostic reports; it must not perform worker actuation through these entrypoints.
