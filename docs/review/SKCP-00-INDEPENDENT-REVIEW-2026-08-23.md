# SKCP-00 independent architecture review

Date: 2026-08-23
Reviewer: `jarvis`
Review card: `d0edbff1`
Candidate card: `9442b3b3`
Verdict: FAIL

## Reviewed version

The reviewed candidate is exactly:

- Manifest: `docs/review/SKCP-00-CANDIDATE-MANIFEST.json`
- SHA-256: `88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`
- Manifest artifacts: 11
- Artifact hash result: 11 of 11 matched
- Human approval record: `docs/approval/SKCP-00-MEASUREMENT-ARCHITECTURE-APPROVAL-2026-08-23.md`

This review made no repair change to the manifest or any candidate artifact.

## Findings

### F1: Unavailable evidence can validate as a numeric zero

Severity: High

`control-plane-metric-result.schema.json` permits `truth_state` to be
`unavailable` while `value` is `0`, coverage is `0/0`, and `errors` is empty.
No conditional schema rule requires an unavailable or unknown result to use a
null value or to carry failure provenance. A conforming producer can therefore
emit the exact false-zero state that the architecture says must be impossible.

Evidence: a Draft 2020-12 validation counterexample with unavailable truth,
numeric zero, zero coverage, and no errors was accepted.

Required disposition: freeze an explicit invariant in the metric contract and
add positive and negative fixtures before Sprint 1 implementation is eligible.

### F2: Recommendation grounding fields may all be empty

Severity: High

`control-plane-recommendation.schema.json` requires the keys
`best_practice_refs`, `expected_impact`, `risks`, `counter_indicators`,
`alternatives`, and `preconditions`, but every array permits zero items. A
recommendation with one opaque metric reference, one opaque evidence reference,
and no practice, impact, risk, alternative, counter-indicator, or precondition
passes validation. This does not enforce the evidence-to-decision contract or
the review card's grounding criterion.

Evidence: a Draft 2020-12 validation counterexample with all six grounding
arrays empty was accepted.

Required disposition: define which recommendation types may omit each field,
enforce non-empty grounding for action-oriented recommendations, and add
abstention fixtures for insufficient evidence.

### F3: A ready action preview may require no approval

Severity: High

`control-plane-action-preview.schema.json` permits `status: ready` with an empty
`required_approvals` array. The OpenAPI authorization description says the
owner service revalidates approvals, but the frozen preview contract does not
express when at least one approval is mandatory. This creates an ambiguous
boundary for high-risk, external, destructive, and protected-Matter actions.

Evidence: a Draft 2020-12 validation counterexample with a ready preview and no
approval entries was accepted.

Required disposition: encode or normatively reference the risk and action
classes that require exact-version approval, and add ready, denied, expired,
and approval-required fixtures.

## Controls that passed review

- All local JSON references resolve and all contract JSON parses.
- The manifest hashes pin the ADR, sprint plan, six contracts, interactive
  wireframe, and two rendered images.
- SKDashboard is a projection plane and the authoritative owner remains named
  for each resource family.
- Missing, stale, partial, unavailable, unknown, and not-applicable truth states
  are separately named at the architecture level.
- Harness and gateway measurement lanes remain separate.
- The insight schema has no capability-token, shell, URL, connector, or generic
  command field.
- Reporting, action preview, and exact-preview authorization are separate API
  operations and scopes.
- The action request requires preview hash, idempotency key, and an approval
  reason.
- Unsupported current routes return 404 and do not fall through to the legacy
  dashboard payload.
- The wireframe is self-contained, labels its data as synthetic, includes
  keyboard and live-region semantics, and supports reduced motion.
- The plan preserves protected SKLegal and HammerTime boundaries and does not
  authorize Inbox access, production deployment, external actions, or raw
  Matter exposure.

## Exact test evidence

```text
python -m pytest -q tests/test_control_plane_architecture.py
8 passed in 0.13s

python -m pytest -q
244 passed, 141 warnings in 17.64s

Draft 2020-12 counterexample validation
counterexample_unavailable_zero=accepted
counterexample_empty_recommendation_grounding=accepted
counterexample_ready_without_approvals=accepted
```

The warnings are existing PGP dependency deprecations and do not affect this
review verdict.

## Gate result

The independent-review gate fails on F1 through F3. Human approval of the exact
candidate remains recorded and auditable, but Sprint 1 leaf cards must remain
blocked until a new attributed candidate revision resolves the findings and a
new independent review passes. This report does not authorize implementation,
deployment, mutation, or external integration.
