# Legacy assignment terminal recovery implementation plan

Goal: unblock admission using exact native assignment and journal custody while preserving producer claims and candidates.

Architecture: admission recognizes a legacy worker's native launch generation, then requires one systemd-manager start and later terminal record for one invocation near that launch. Cache a distinct immutable legacy terminal receipt; do not invent start or observed receipts. Route future dispatcher starts through existing start_reserved.

Constraints: no production installs, source edits to sklegal, claim release, runtime quota changes, absent-unit-only discharge or unqualified review. Main rollout belongs to lumina-nor.

- [ ] Add failing recovery and refusal tests in tests/fleet/test_legacy_assignment_terminal.py. Run PYTHONPATH=src python -m pytest -q tests/fleet/test_legacy_assignment_terminal.py and retain the red result.
- [ ] Implement src/skcapstone/fleet/production_legacy_terminal.py with native launch, bounded journal, stable absence and immutable receipt validation. Integrate before prestart reconciliation in production_admission._occupancy.
- [ ] Route the legacy dispatcher through start_reserved, preserving non-production launch behavior and retaining custody on fence failure. Add an executable extracted-branch test.
- [ ] Add changelog and exact observed incident evidence. Commit before long verification.
- [ ] Run affected admission and dispatcher suites plus Black, Ruff, diff and changelog checks. Fix failures in forward commits. Push one PR and enable rebase auto-merge. No candidate installation.
