# SKCP-00 measurement architecture candidate evidence

Date: 2026-08-23
Agent: codex-root
Card: `9442b3b3`
Epic: `6f7fd828`
Status: Candidate ready for human review, implementation not authorized

## Outcome

Produced an implementation-ready measurement and reporting candidate for the
SKDashboard control plane, including:

- Authoritative-owner and projection boundaries across the full SK estate
- Canonical `/api/v1` direction and compatibility rules
- Metric envelope, truth states, measurement kinds, scope, watermarks, and data
  quality
- Immutable report snapshots
- Governed AI insights, conclusions, best-practice recommendations, expected
  impact, confidence, counter-indicators, alternatives, risks, and preconditions
- One-click handoff from a recommendation to a deterministic authorization
  preview, followed by a separate explicit exact-hash approval click
- Six breadth-first sprint containers and 22 dependency-gated implementation
  leaf cards
- Interactive and rendered visual wireframes

The exact candidate is pinned by
`docs/review/SKCP-00-CANDIDATE-MANIFEST.json`, sha256
`88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`.

## Files changed

- `docs/architecture/ADR-0001-CONTROL-PLANE-MEASUREMENT-AND-REPORTING.md`
- `docs/planning/SK-CONTROL-PLANE-BREADTH-FIRST-SPRINTS.md`
- `docs/contracts/control-plane-metric-result.schema.json`
- `docs/contracts/control-plane-report-snapshot.schema.json`
- `docs/contracts/control-plane-insight.schema.json`
- `docs/contracts/control-plane-recommendation.schema.json`
- `docs/contracts/control-plane-action-preview.schema.json`
- `docs/contracts/openapi.control-plane.v1.json`
- `docs/review/SKCP-00-CANDIDATE-MANIFEST.json`
- `docs/wireframes/control-plane-estate-pulse.html`
- `docs/wireframes/control-plane-estate-pulse.png`
- `docs/wireframes/control-plane-authorization-preview.png`
- `docs/evidence/SKCP-00-CANDIDATE-2026-08-23.md`
- `tests/test_control_plane_architecture.py`

No existing runtime source file was changed.

## Board evidence

- Claimed exact architecture card `9442b3b3` as codex-root.
- Created planning-only sprint containers `84f763ac`, `4747a506`, `973b802f`,
  `ef218eb9`, `e3cef4b4`, and `7aad8af1`.
- Created human review gate `9508b8fd`.
- Created independent review `d0edbff1`.
- Created canonical-card confirmation gate `d79100a7` after verifying Jarvis had
  already archived duplicate `5ae27468` and obsolete `f3672bc4`.
- Created 22 leaf cards, SKCP-11 through SKCP-52 as listed in the candidate
  manifest and sprint plan.
- Audited every new dependency against the coordination task store. Missing new
  dependencies: zero.
- Added sprint and gate labels to the active legacy SKCP cards without changing
  their owners, descriptions, dependencies, or workflow state.
- Linked the manifest, ADR, OpenAPI, sprint plan, and wireframes to SKCP-00, the
  human gate, the sprint containers, and the epic.

No ambiguous card was voided, completed, or rewritten.

## Tests and exact results

Commands:

```text
ruff check tests/test_control_plane_architecture.py
```

Result: `All checks passed!`

```text
~/.skenv/bin/python -m pytest tests/test_control_plane_architecture.py -q
```

Result: `8 passed in 0.10s`

```text
~/.skenv/bin/python -m pytest tests/ -q
```

Result: `244 passed, 141 warnings in 18.16s`

The warnings are existing PGPy and cryptography deprecations. No test failed.

The architecture tests verify:

- JSON parseability and local reference integrity
- Required OpenAPI read, report, insight, action-preview, authorization, and
  streaming paths
- Explicit metric truth states and measurement kinds
- Required AI recommendation evidence and decision fields
- Exact authorization-preview, Approval, verification, and rollback fields
- Self-contained accessible synthetic wireframe behavior
- Exact candidate-manifest hashes
- ASCII-only dashes in candidate text artifacts
- Unknown GET and POST routes return 404 and do not fall through to an overview
  or unrelated endpoint

Browser render evidence:

- `control-plane-estate-pulse.png`: 1600 by 1200 RGB PNG
- `control-plane-authorization-preview.png`: 1600 by 1200 RGB PNG

## Acceptance evidence

SKCP-00 candidate criteria are addressed as follows:

1. Canonical process, local origin, API namespace, and resource owners are named
   in ADR-0001.
2. OpenAPI and JSON Schema fixtures cover Board, fleet, Economy, health, errors,
   freshness, pagination, ETag, reports, AI insights, action previews,
   authorization receipts, and event streaming.
3. The threat analysis covers confused deputy, route fallback, capability
   leakage, replay, cross-origin access, oversized input, unbounded scans, stale
   and partial projection, unauthorized mutation, prompt injection, protected
   Matter disclosure, sensitive telemetry, metric gaming, and definition drift.
4. Current unknown GET and POST routes return a non-success 404 and are covered
   by a regression test. The candidate requires JSON error envelopes for new
   `/api/v1` runtime routes.
5. Contract, reference, hash, wireframe-boundary, ASCII-dash, and route-failure
   tests execute in CI-compatible pytest form.

## AI and authorization boundary evidence

- Deterministic metric calculations precede any model call.
- AI output is a typed proposal or abstention and links metrics, calculations,
  evidence, best-practice versions, uncertainty, contradictions, and model
  provenance.
- Recommendations identify impact range, horizon, confidence, risks,
  counter-indicators, alternatives, preconditions, reversibility, and a
  preview-only next step.
- The reporting query contract has no command tool, bearer capability, arbitrary
  URL, shell, filesystem, email, filing, calendar, or connector input.
- The action registry is closed and versioned.
- Opening an authorization preview is not Approval.
- Exact-hash authorization revalidates actor, capability, purpose, owner policy,
  expected version, Approval, expiry, and idempotency.
- Protected Matter and external actions retain their existing state machines and
  exact-version human gates.

## Known limitations and open gates

- Human review card `9508b8fd` is open. Silence or partial feedback is not
  Approval.
- Independent review `d0edbff1` is blocked on the architecture candidate.
- Board confirmation `d79100a7` is blocked on the architecture candidate.
- SKCP-00 remains in progress and is not marked complete.
- `/api/v1` runtime endpoints, metric registry, adapters, live UI, reports, AI
  service, action preview, and authorization execution are not implemented.
- The wireframe uses synthetic data and sends no action.
- No production endpoint, tailnet ingress, deployment, external account,
  connector, or report destination was changed.
- No SKLegal protected Matter content was read or moved.
- HammerTime Inbox was not searched, read, moved, or processed.
- The separate dirty SKLegal worktree was not changed by this task.

## Migration and rollback

There is no data or runtime migration.

If the human rejects the candidate:

- Source artifacts can be superseded by a new attributable candidate while this
  manifest remains as evidence.
- New board cards can be append-only voided with an exact rejection reason.
- Sprint labels and links can be append-only amended or removed.
- No production state, source record, action, report destination, or external
  effect requires rollback.

## Next gate

The next action is explicit human review of `9508b8fd` against the pinned
manifest. Approval or requested changes should address the eight decisions in
ADR-0001 and the seven decisions in the sprint plan. Implementation remains
blocked until the human gate and independent review pass.
