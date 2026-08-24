# SKCP-00F2 UX wireframe completion evidence

Date: 2026-08-23
Agent: jarvis
Card: `0242f9f2`
Parent: SKCP-00 approved measurement architecture

## Outcome

Added a versioned V2 synthetic wireframe package without changing any
manifest-pinned candidate artifact. The V2 package covers the approved
breadth-first role, evidence, least-click, accessibility, and authorization
preview requirements.

## Files changed

- `docs/wireframes/control-plane-estate-pulse-v2.html`
- `docs/wireframes/control-plane-estate-pulse-v2.png`
- `docs/wireframes/control-plane-authorization-preview-v2.png`
- `tests/test_control_plane_ux_acceptance.py`
- This completion evidence file

No contract, manifest, ADR, sprint plan, or existing architecture test was
changed. The original 11 manifest artifact hashes were recomputed and matched.

## Acceptance evidence

The dedicated UX test verifies:

1. Twelve silo rows with explicit owner and truth state, including stale,
   unavailable, unknown, and policy-filtered states.
2. Direct project-manager blocked-value and architect blast-radius paths with
   estate scope, time window, and baseline preserved in each target.
3. Real `/control-plane/...` targets for primary navigation, role paths,
   signal tiles, configuration, and data fixes. Placeholder navigation is not
   used. Evidence opens a read-only detail drawer with metric references,
   calculations, source watermark, coverage, truth state, and uncertainty.
4. AI transparency fields for metric references, uncertainty,
   counter-indicators, alternatives, preconditions, impact horizon, proposal
   status, and abstention.
5. Authorization preview fields for actor, capability, exact target version,
   policy decision reference, exact hash, expiry, verification, rollback,
   stale-target behavior, denial behavior, and no-dispatch boundary.
6. Skip link, dialog semantics, modal state, focus return, Tab trapping,
   Escape close, table caption and column scopes, responsive breakpoints,
   reduced-motion behavior, truth-state legend, and one or two interaction
   budgets.

## Tests and exact results

```text
python -m pytest tests/test_control_plane_ux_acceptance.py -q
7 passed in 0.02s

ruff check tests/test_control_plane_ux_acceptance.py
All checks passed!

forbidden dash scan on V2 HTML and UX test
clean

V2 render checks
control-plane-estate-pulse-v2.png: 1600 x 1200 RGB PNG
control-plane-authorization-preview-v2.png: 1600 x 1200 RGB PNG

manifest hash verification
clean
```

The broader suite result was `263 passed, 1 failed, 143 warnings`. The single
failure is the unchanged `test_control_plane_contract_json_is_parseable_and_local_refs_exist`
assertion, which expects exactly six contract files while unrelated v1.1.0
contract files are present in the shared worktree. No V2 UX test failed.

## Migration and rollback

No runtime, data, contract, or deployment migration exists. The change is
additive. Rollback is removal or supersession of the four V2 evidence and
wireframe files, leaving the original manifest-pinned artifacts unchanged.

## Known limitations

- V2 remains a synthetic wireframe and does not call live APIs.
- Deep-link paths document the implementation contract and are not runtime
  routes until the later workspace cards implement them.
- The old architecture test remains incompatible with the unrelated v1.1.0
  contract additions and was intentionally not edited.
