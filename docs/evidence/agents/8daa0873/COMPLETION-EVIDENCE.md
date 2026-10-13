# Card 8daa0873 — Completion Evidence

## Scope and controls

This candidate repairs coordination admission and gate readback only. It uses a
fresh authoritative `CardStore` fold, consults authoritative void events, and
shares structural admission reasons between gate diagnostics, eligibility
selection, and local claim entrypoints. Claim writes remain behind the existing
`skcapstone coord`/Board boundary. No worker termination, provider change,
deployment, credentials, protected data, installation, or card completion was
performed.

The working tree was based directly on `8c8a2a2284ae8f9bdcd3e6562ac65de87ca0376d`
on branch `feature/8daa0873-admission-gates`.

## Implementation

- `coord_gate_diagnostic.py` now reports stable holds for missing or
  non-terminal dependencies, `do-not-claim`, `human-gate`, terminal cards, and
  authoritative voids, and exposes the fail-closed claim assertion.
- All local claim paths (CLI, MCP, shell, auction, spawner, and builder
  dispatch) assert admission immediately before `Board.claim_task`.
- Builder dispatch reuses its fresh folded card and treats admission refusal as
  a retry/blocked request rather than authorizing a stale claim.
- Leaf eligibility excludes voided cards and cards whose dependencies are
  voided, keeping selectors aligned with gate readback.
- Tests cover dependency, labels, voids (including done-then-void), unknown and
  terminal behavior, concurrent/idempotent reads, status filtering, and
  dispatch behavior.

## Verification

Focused changed-surface run:

```text
152 passed
```

Command:

```text
python -m pytest -q \
  tests/test_coord_gate_diagnostic.py tests/test_coord_status_filters.py \
  tests/test_dependency_blocker_claim_gate.py tests/test_void_completed_card.py \
  tests/test_cli_coord_create_claim.py tests/test_coord_card_mcp.py \
  tests/fleet/test_builder_dispatch.py tests/fleet/test_scheduler_filter.py \
  tests/fleet/test_scheduler_place.py tests/fleet/test_scheduler_preference.py \
  tests/test_spawner.py
```

Additional checks:

- `ruff check` on all ten changed Python files: passed.
- `ruff format --check` on all ten changed Python files: passed (`10 files
  already formatted`).
- `python -m compileall -q src`: passed.
- `git diff --check`: passed.
- Secret-delta scan with gitleaks against the uncommitted patch: passed, no
  leaks found. A direct source-diff secret-like pattern scan also found no
  matches.
- Worker Git identity was available from the governed environment:
  `pi-gateway-builder-node-chiap01-8daa0873` /
  `pi-gateway-builder-node-chiap01-8daa0873@noreply.invalid`.

The scheduler-place file passes independently. When included in a broader
aggregate invocation it previously produced two setup errors because the
aggregate invocation did not provide its `paths` and `operator` fixtures; the
focused changed-surface invocation above supplies the valid result (`9 passed`)
and has no such error.

Board reads can emit pre-existing warnings about malformed non-card-event lines
in `fleet-liveness-reaper.jsonl` (approximately 297 lines). The requested
folded-card and gate outputs remain available; no board data was edited.

## Reproduction and rollback notes

The new tests construct authoritative folded state and verify that a
non-terminal dependency, `do-not-claim`, `human-gate`, and authoritative void
all prevent eligibility/admission. They also verify repeated concurrent reads
are stable and that a stale projection cannot authorize a claim.

Rollback is a normal source revert of the candidate commit. No runtime or
installation rollback was required or executed. A clean temporary checkout
verified binary patch reconstruction from the base and reverse-application
restored the base tree exactly (`reconstruction apply: PASS`; `rollback
reverse-apply: PASS`). No external artifact or credential is required.

## Candidate integrity

The ten source/test files were hashed before commit as follows:

```text
src/skcapstone/auction.py                         06b95fefeafd2a521b32223633a9178f8ed9c2de35fc1f759b65cdadd8ea7998
src/skcapstone/cli/coord.py                       da3ea86dd8da97cba9a03904216661ad4ce69b336ed5107da834700f492096e5
src/skcapstone/coord_eligibility.py               71ae9e3f6da34932cded89a27be4af7a70cef334a0507e86a723e9d07823a9ab
src/skcapstone/coord_gate_diagnostic.py           d5663bed43b381b75db8fb7a14ef5b578685bcbdea4f7a9680f2084ae490f0c4
src/skcapstone/fleet/builder_dispatch.py          a9881976bb9350d4e680821f5de46f9ad916eef2570e62267bea66ed64a05455
src/skcapstone/mcp_tools/coord_tools.py           10379300a65c9d531b7dcae8ea6bf596abc4d49c4a001a5b7eaf0c7da7704dca
src/skcapstone/shell.py                            0d5b188343f2993e78217692a4555e2f56c1d179a6f2a92c1cbe5aa254094a05
src/skcapstone/spawner.py                          9ba7ea9bafd5b8e6c0cb1997135a3e8d52a5ddf67a4a7e085bfa56063fc0ee9d
tests/test_coord_gate_diagnostic.py                881ae8715fa1e7cab27da9978d09a91841654eaa2e08eb7dcde626e551e3e28b
tests/test_coord_status_filters.py                 7cfe2b6b868cec83e39f5406ed752abfb4cf7dfc11cca0b131dc09a5db16e151
```

The final candidate commit and tree IDs are recorded after commit in the
handoff message and coordination evidence links. Independent review is
required before any installation; this evidence does not constitute review.
The evidence file itself is included in the candidate commit, so its final
hash is recorded by the committed tree rather than the pre-commit source hash
list above.
