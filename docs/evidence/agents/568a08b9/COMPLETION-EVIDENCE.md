# Production integration baseline

Card: `568a08b9`. Producer: `codex-production-baseline-568a08b9`.

This is an integration source candidate for parent9b230773. No services, launch policies, credentials, claims belonging to other cards, or installed runtime files were changed. Fiber stays outside this composition.

## Exact inputs

Historical production base: `32fa67d5c783ed31bf766e85cd6ce3530d81eae7`, tree `1c34e1096f4cac3073b479a6777f60edda0a37d3`.

- `scripts/fleet/skfleet-rotate.py`: preserve that base and apply only the two previously reviewed005ec3ec DRY guards. The resulting body exactly equals installed `~/.skenv/bin/skfleet-rotate.py`; only the installation interpreter shebang differs. Installed SHA256: `36099f625c31736ce72b261630e3973b10a8ebe30896e1b443beb2bcd8c1d747`. Prior receipt: `~/.skcapstone/evidence/work/3ee76566/005ec3ec-installation.json`.
- `src/skcapstone/fleet/review_capacity.py`: exact installed reviewed9bb source, SHA256 `14bc401e9c25e5428778ba11ef47ed69910fc496f3401cd302ae541e62c5f7d7`.
- `src/skcapstone/review_admission.py`: exact installed reviewed9bb source, SHA256 `d24b9944d4ee757fd3dd6a24ad077ba943d81209f9df8114d33cf09be376309c`.
- Native overlay input commit: `9bb5d51e9a62811aa4cc0ff9d9f63156a3534a63`; installation receipt `~/.skcapstone/evidence/work/f34f26c5/install-native-receipt-1790824548123246570.json`.

No replacement with the different `~/.local/bin` legacy dispatcher occurred. No later installed native completion modules are reverted by this source composition. A full wheel reinstall is not authorized by this candidate.

## Validation

The seven production activation, entrypoint, recovery, exclusion and placement suites plus builder dispatch/void fencing, native gateway review capacity/admission, diagnostic parity, and the005 dry-run behavioral suite passed **243 tests, 0 skips, exit0, 7.03seconds**.

Command:

```
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src ~/.skenv/bin/python -m pytest -p no:cacheprovider -q tests/test_niobe_activation.py tests/test_niobe_live_entrypoint.py tests/test_seat_cycle_orchestrator.py tests/test_seat_cycle_orchestrator_budget.py tests/test_seat_cycle_recovery.py tests/test_seat_cycle_exclusion.py tests/test_skfleet_niobe_builder_reachability.py tests/fleet/test_builder_dispatch.py tests/fleet/test_builder_dispatch_voided_card.py tests/fleet/test_review_capacity.py tests/fleet/test_review_gateway_only.py tests/fleet/test_rotation_dry_safety.py tests/test_coord_gate_diagnostic.py
```

Final log: `~/.skcapstone/evidence/work/568a08b9/baseline-tests-final.log`, SHA256 `c7314b367e8fe46787a5058407cb07baec742b299ce01b8b2b6bf818f5c485bc`.

Initial log is retained:236PASS/7FAIL because the old diagnostic fixture used a future timestamp rejected by the deployed freshness gate. The corresponding already-reviewed9bb test fixture uses current time and was included unchanged. No production freshness check was relaxed. The old literal builder-guard assertion now names its reviewed DRY guard; actual dry/live builder and watchdog tests cover behavior.

`git diff --check` passed. Source comparisons bind all three production files to installed bytes as stated above. Final candidate head/tree are bound externally by `CANDIDATE.json`; this committed report does not try to contain its own final hash.

## Remaining work and rollback

This candidate provides a coherent production source baseline, not proof of restored production throughput. Existing Codex-only Niobe policy, dispatcher scheduling caps, builder count caps and authoritative placement need separately claimed changes. Exact cross-family review is pending; the current claim remains held. No push or deployment occurred. Rollback at this stage is choosing the prior source commit; original and deployed files remain untouched.
