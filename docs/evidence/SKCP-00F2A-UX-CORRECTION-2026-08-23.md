# SKCP-00F2A V2 UX correction evidence

Date: 2026-08-23
Agent: jarvis
Card: `f94fde82`
Supersedes: `docs/evidence/SKCP-00F2-UX-WIREFRAME-2026-08-23.md` for the
independent audit corrections

## Outcome

Corrected the three independent V2 UX blockers without changing any original
manifest-pinned candidate artifact. The V2 wireframe now has silo-correct
evidence routing, live scope and role context propagation, and executable
synthetic authorization preview states.

## Files changed

- `docs/wireframes/control-plane-estate-pulse-v2.html`
- `docs/wireframes/control-plane-estate-pulse-v2.png`
- `docs/wireframes/control-plane-authorization-preview-v2.png`
- `tests/test_control_plane_ux_acceptance.py`
- This F2A evidence file

No original wireframe, contract, manifest, ADR, sprint plan, or unrelated
SKCounter file was changed.

## Acceptance evidence

1. All twelve evidence keys now have distinct synthetic metric, calculation,
   owner, source watermark, truth state, coverage, uncertainty, and drill
   target data. Unknown keys fail with a visible synthetic evidence message and
   do not fall back to another silo.
2. Scope, time window, baseline, and active role controls update relevant
   `/control-plane/` links. Role-specific cards retain their intended role,
   while navigation, tiles, saved views, data-quality links, and evidence
   drill targets receive the selected role context.
3. The preview state selector executes `ready`, `stale-target`,
   `denied-policy`, `expired`, and `changed-parameters`. Unsafe states visibly
   update status and reason text, set `disabled`, set `aria-disabled`, and
   prevent authorization handling.
4. A persistent active-role selector is visible in the global control bar.
5. Focus trapping, Escape handling, focus restoration, table labels, reduced
   motion, responsive breakpoints, truth-state coverage, and no-dispatch
   behavior remain covered.

## Tests and exact results

```text
python -m pytest tests/test_control_plane_ux_acceptance.py -q
10 passed in 1.87s

python -m pytest tests/ -q
267 passed, 143 warnings in 19.25s

ruff check tests/test_control_plane_ux_acceptance.py
All checks passed!

forbidden dash scan on V2 HTML and UX test
clean

Original manifest hash verification
11 of 11 artifacts verified

V2 render checks
control-plane-estate-pulse-v2.png: 1600 x 1200 RGB PNG
control-plane-authorization-preview-v2.png: 1600 x 1200 RGB PNG
```

The focused suite includes Chrome headless DOM checks for dynamic scope and
role links plus each authorization state and its disabled reason.

## Migration and rollback

No runtime, data, contract, or deployment migration exists. The correction is
limited to the additive V2 package and its acceptance test. Rollback is
removal or supersession of the V2 files, leaving all original manifest-pinned
artifacts byte-exact.

## Known limitations

- V2 remains synthetic and does not call live APIs.
- Deep-link paths document the future workspace contract until runtime cards
  implement those routes.
- The original F2 evidence remains preserved as an attributable prior record.
