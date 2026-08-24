# SKCP-00F9 schedule truth repair evidence

Date: 2026-08-24
Card: `f30e9e0a`
Mandatory rereview: `39085b32`
Repository: SKDashboard

## Outcome

The append-only repair keeps source truth, visibility, and authorization as
separate dimensions. Policy-filtered or unauthorized evidence is never mapped
to `not_applicable`. Closed dialogs are removed from keyboard focus with both
`hidden` and `inert`, and the Ask AI button meets normal-text contrast.

The approved V1.1.2 candidate, receipt, approval, and pinned source artifacts
remain unchanged.

## Preserved approved inputs

- `docs/review/SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.1.md`
  SHA-256 `88172dd498f3071d7665dd1f5e37933dd229d808c6f3cc78b0ace14ce1b9b0ff`
- `docs/wireframes/control-plane-estate-pulse-v2.html`
  SHA-256 `66d007a9f1339929666e2a34586c1d49eb7e3d6236d83d11f43a449cf02b4c63`

## Superseding active artifacts

- `docs/review/SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.2.md`
  SHA-256 `b1f05fd98aa1d9dc940302321efcf57b5209a8020a1cff02ab658b3e5ec0911e`
- `docs/wireframes/control-plane-estate-pulse-v2.1.html`
  SHA-256 `b3636c0017f5f3289094873b0ebed03806fbaa3bbc92bc705e03e0f7c32037c9`

## Sensitivity and browser evidence

The focused tests deliberately apply the new invariants to the approved old
artifacts and observe rejection. They then validate the repaired artifacts.
The original Ask AI colors measure 2.774:1 contrast; the repaired dark text on
the same purple background measures 6.840:1.

A separate Chrome DevTools Protocol session loaded the V2.1 wireframe at 1440
by 1100 pixels and verified:

- both closed dialogs report `hidden=true`, `inert=true`, and
  `aria-hidden=true`;
- eighteen Tab operations reach no hidden drawer control;
- the legal signal renders source truth `unknown` and separate
  `policy_filtered; authorization denied` visibility;
- opening evidence removes `hidden` and `inert`, sets `aria-hidden=false`, and
  focuses the evidence close button;
- the unopened authorization drawer remains hidden and inert;
- clicking the synthetic authorization control creates no non-GET request and
  no external request;
- the page reports no JavaScript exception or network loading failure other
  than the local temporary server's missing favicon.

Screenshot evidence was captured locally at
`/tmp/skdashboard-f9-v2.1-evidence.png`. The screenshot is not a contract input
and is intentionally not committed.

## Verification commands

```text
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider -q tests/test_control_plane_schedule_truth_repair.py tests/test_control_plane_v1_1_2_candidate.py tests/test_control_plane_ux_acceptance.py
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider -q
PYTHONDONTWRITEBYTECODE=1 python -m ruff check .
PYTHONDONTWRITEBYTECODE=1 python -m ruff format --check tests/test_control_plane_schedule_truth_repair.py
```

## Boundaries

This repair is a synthetic visual and requirements correction. It does not
implement the production Gantt workspace, retrieve protected data, dispatch an
action, deploy a service, complete `39085b32`, or make any downstream
implementation card eligible.
