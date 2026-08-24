# SKCP-00F2B V2 service scope and truth summary evidence

Date: 2026-08-23
Agent: jarvis
Card: `b24213ea`
Supersedes: `docs/evidence/SKCP-00F2A-UX-CORRECTION-2026-08-23.md` for the
service scope and truth summary correction

## Outcome

Added service scope as a first-class V2 context value while preserving estate
scope, time window, baseline, and role. The V2 URL, browser history, global
links, role paths, and evidence drills now carry the same complete context.
The visible truth summary now uses an explicit denominator of twelve visible
silos and exactly matches the twelve synthetic rows.

## Files changed

- `docs/wireframes/control-plane-estate-pulse-v2.html`
- `docs/wireframes/control-plane-estate-pulse-v2.png`
- `docs/wireframes/control-plane-authorization-preview-v2.png`
- `tests/test_control_plane_ux_acceptance.py`
- This F2B evidence file

No original pinned artifact, contract, manifest, ADR, sprint plan, or
unrelated SKCounter file was changed.

## Acceptance evidence

1. The service selector is included in `currentContext`, initialized from the
   URL, written to browser history on selector changes, restored on `popstate`,
   and propagated to every relevant `/control-plane/` deep link and evidence
   drill. Estate scope, window, baseline, and role remain in each target.
2. Browser interaction coverage initializes all five context fields from a
   query string and asserts a scoped navigation link and a scoped legal
   evidence drill. The test also asserts the visible service selector and its
   SKGateway option.
3. The truth summary has an explicit twelve-row denominator and the exact
   distribution `5 current`, `3 partial`, `1 unavailable`, `1 stale`,
   `1 unknown`, and `1 policy filtered`. The summary is labeled `12 visible
   silos` and its accessible label names the same denominator.

## Tests and exact results

```text
python -m pytest tests/test_control_plane_ux_acceptance.py -q
11 passed in 2.20s

python -m pytest tests/ -q
268 passed, 143 warnings in 19.32s

ruff check tests/test_control_plane_ux_acceptance.py
All checks passed!

forbidden dash scan on V2 HTML, UX test, and this evidence file
clean

Original manifest hash verification
11/11 original manifest hashes match

V2 render checks
control-plane-estate-pulse-v2.png: 1600 x 1200 RGB PNG
control-plane-authorization-preview-v2.png: 1600 x 1200 RGB PNG
```

The focused suite includes Chrome headless assertions for URL initialization,
service scope propagation, role preservation, evidence drill propagation,
truth summary counts, and authorization preview behavior.

## Migration and rollback

No runtime, data, contract, or deployment migration exists. The correction is
limited to the additive V2 package and its acceptance test. Rollback is
removal or supersession of the V2 files, leaving all original manifest-pinned
artifacts byte-exact.

## Known limitations

- V2 remains synthetic and does not call live APIs.
- Deep-link paths document the future workspace contract until runtime cards
  implement those routes.
- Earlier F2 and F2A evidence remains preserved as attributable prior records.
