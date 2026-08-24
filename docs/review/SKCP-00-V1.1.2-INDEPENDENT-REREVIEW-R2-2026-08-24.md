# SKCP-00 V1.1.2 independent rereview

Date: 2026-08-24
Reviewer: Independent contract reviewer
Reviewed revision: `3e443a79683995d3d27b5f410788f4335ec41ccf`
Reviewed release: `v0.1.26`
Reviewed manifest SHA-256: `257db46aa26297873cd6a769e3f0eb7e6e3cf756224f99ef9a3aad61a45ff5ab`
Reviewed receipt SHA-256: `46b98341094cf06a5f260c0ad1eed1e8d3a0090f27c2f8d570dcb84312028749`
Verdict: **FAIL**

The reviewer made no repository, board, claim, repair, publication, or external
action mutation.

## Blocking findings

### R2-B01: Policy visibility collapsed into source truth

`docs/review/SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.1.md` maps a
policy-filtered record to `not_applicable` presentation. The active
`docs/wireframes/control-plane-estate-pulse-v2.html` repeats the mapping in the
legal signal, truth-state legend, and evidence data.

This contradicts
`docs/contracts/CONTROL-PLANE-CONTRACT-COMPATIBILITY-v1.1.0.md`, which requires
`visibility.state: policy_filtered` with denied or unknown authorization while
preserving source truth. `not_applicable` is reserved for a metric explicitly
outside scope.

Impact: hidden or unauthorized schedule evidence can appear irrelevant rather
than policy-filtered, affecting UI, export, aggregation, forecast, scenario,
and AI explanation boundaries.

### R2-B02: Closed dialogs remain keyboard-focusable

Both active V2 dialogs initialize with `aria-hidden=true` but are only moved
offscreen. They lack `hidden` and `inert`. Static DOM analysis found seven
focusable descendants under hidden ancestors:

- `evidence-close`
- `evidence-drill`
- `evidence-cancel`
- `auth-close`
- `preview-state`
- `auth-cancel`
- `authorize`

Impact: initial Tab navigation can reach invisible controls that are omitted
from the accessibility tree.

### R2-B03: Ask AI normal text fails contrast

The active V2 Ask AI button renders 10-pixel white text on `#9a8cff`.
Independent WCAG relative-luminance calculation produces 2.774:1, below the
4.5:1 normal-text requirement. Existing UX tests contain no numeric contrast
sensitivity check.

### R2-B04: Premature terminal review completion exposed implementation

During the audit, the authoritative fold briefly showed `d0edbff1` terminal
done before a valid rereview artifact existed. Four implementation cards then
had all dependencies done: `9e88de5c`, `804f14de`, `d12b8951`, and
`94cbf19a`. The reviewer attempted no claim.

Append-only recovery subsequently created repair card `f30e9e0a` and mandatory
rereview card `39085b32`, then added `39085b32` exactly once to all 29 V1.1.1
implementation roots. Read-only recovery verification found zero eligible
implementation cards. That recovery does not change this verdict against the
pre-repair release.

## Passing evidence

- The manifest and receipt hashes recomputed exactly.
- All 21 directly pinned artifact paths matched.
- Six active JSON documents and 132 local references validated with zero
  resolution errors.
- All Draft 2020-12 schemas were valid.
- The 185-case schema adversarial matrix had zero mismatches.
- Missing or failed evidence could not become numeric zero or healthy.
- AI query contracts rejected injected canonical math, result, action,
  capability, shell, URL, filesystem, and connector fields.
- Typed insight rejected execution and capability material.
- Preview and authorization remained separate schemas and endpoints.
- Grounding, policy, abstention, exact target, exact version, and exact approval
  guards held.
- Seven focused control-plane test files passed 72 tests, including Chrome.
- The full suite passed 309 tests with 143 warnings.
- Ruff passed for source and tests.
- Approval PR #32, main CI, docs, gitleaks, GitGuardian, build, and both Python
  test jobs were successful.

## Limitations

This rereview covers architecture, contracts, tests, and a synthetic
wireframe. Source adapters and the production Gantt workspace do not yet exist,
so they cannot be runtime-qualified. No claim probe was attempted and no
secret value was exposed.

## Required recovery

Repair `f30e9e0a` must correct all three active visual boundaries while keeping
the approved V1.1.2 bytes immutable. Independent rereview `39085b32` must then
publish an exact PASS or FAIL before any implementation leaf becomes eligible.
