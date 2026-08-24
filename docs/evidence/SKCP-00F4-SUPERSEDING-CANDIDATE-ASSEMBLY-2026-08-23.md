# SKCP-00F4 superseding candidate assembly evidence

Card: `90a02b0e`
Date: 2026-08-23
Status: all acceptance checks passed; board completion follows this evidence link.

## Scope and preservation

This assembly created a distinct superseding package. It did not alter any
artifact pinned by the original manifest.

- Original manifest:
  `docs/review/SKCP-00-CANDIDATE-MANIFEST.json`
- Original SHA-256:
  `88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`
- Superseding manifest:
  `docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.0.json`
- Superseding manifest SHA-256:
  `6b35f9e77f8f51dde5243bd9ebc5f55adbf65141d344218b165845bd3475a194`

The original remains valid for audit and is marked superseded, not replaced.

## Files assembled

- `docs/architecture/ADR-0001-CONTROL-PLANE-MEASUREMENT-AND-REPORTING-v1.1.0.md`
- `docs/planning/SK-CONTROL-PLANE-BREADTH-FIRST-SPRINTS-v1.1.0.md`
- `docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.0.json`
- `tests/test_control_plane_superseding_candidate.py`
- this evidence record

The superseding manifest pins 25 exact-hash artifacts: the V1.1 ADR and
plan amendments, compatibility note, five V1.1 schemas, V1.1 OpenAPI, V2 HTML
and PNG assets, all F1 through F3E evidence records, the original independent
FAIL review, and the dedicated assembly test.

## Acceptance evidence

- The ADR and plan give a distinct, correct disposition for F1, F1A, F2, F2A,
  F2B, F3, F3A, F3B, F3C, F3D, and F3E. F3 is the independent-review
  dependency gate correction. F3B is the dependency and claim-release
  atomicity correction.
- The plan contains the layered Now, Roadmap, Gantt, Flow, Forecast, Scenario,
  and AI action-preview design. It cites direct official GitHub Projects,
  Azure Delivery Plans, Jira timeline and Plans, and Kanban Guide sources.
- The plan and ADR map UI truth, service, preview, and proposal states to V1.1
  contract fields. The mapping preserves owner-service authority and prevents
  a UI label, planning projection, AI output, or preview from becoming
  execution.
- The catalog preserves all 22 original leaf cards and adds exactly
  `SKCP-21A` `eddaa1fb` and `SKCP-30A` `7888e091`, with each card's
  exact folded dependencies.
- The manifest records the current F4 effective dependency fold and all seven
  append-only add-dependency events. Read-only installed Kanban confirmed all
  eleven effective dependencies are done.
- The human gate remains `bea13a70` in backlog. Independent review remains
  `d0edbff1` in review. The superseding candidate status remains
  `proposed_for_human_review` and `implementation_authorized` is false.
- The manifest distinguishes historical original FAIL results from fresh
  remediation results and records the shared-live-board global parity
  limitation without reconciling or rewriting it.

## Verification

Focused candidate, contracts, V2, and architecture verification:

```text
43 passed, 2 warnings in 2.59s
```

Final full dashboard suite under a 300-second bound:

```text
280 passed, 143 warnings in 22.86s
```

The warnings are existing PGP and JSON-schema resolver deprecations. No test
failed.

Additional checks:

```text
ruff check tests/test_control_plane_superseding_candidate.py
All checks passed!

original manifest SHA-256 check
OK

superseding artifact hash check
25 matched

Draft 2020-12 and local OpenAPI reference checks
passed by dedicated test

forbidden U+2013 and U+2014 scan of new ADR, plan, manifest, and test
no matches

installed read-only Kanban
F4 dependencies all done
bea13a70 backlog
d0edbff1 review
```

The accepted board-runtime evidence remains applicable and is pinned by the
superseding manifest:

```text
SKCoord: 406 passed in 3.79s
bounded SKCapstone: 6405 passed, 38 skipped, 554 warnings in 357.85s
```

No SKCoord or SKCapstone source changed during F4 assembly.

## Global parity limitation

The manifest records the shared live read-only parity result from the accepted
board-runtime evidence: 985 checked, 590 matched, 125 mismatches, 270 missing,
and open drift 10. This pre-existing global projection discrepancy was not
migrated, reconciled, or rewritten. The isolated installed parity proof remains
clean as documented in F3D and F3E.

## Rollback and non-authorizations

Rollback is rejecting this superseding candidate and retaining the original
manifest for audit. No data migration occurred. The read-only board fold can be
checked with:

```text
SKCOORD_CARD_STORE=0 skcapstone coord kanban --json
```

This work does not authorize implementation source, leaf-card work, deployment,
production activation, service restart, commit, push, external action, product
integration, Matter access, protected-data retrieval, HammerTime corpus access,
HammerTime Inbox access, a successful implementation claim probe, completion
of `bea13a70`, or completion of `d0edbff1`.
