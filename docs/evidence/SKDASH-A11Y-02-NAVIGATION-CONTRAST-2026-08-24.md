# SKDASH-A11Y-02 navigation contrast evidence

Card: `cc3bedc3`

Base: `origin/main` at `aa2529c7d5ac7794e58b479c20e9bf5ca9e64d51`

## Change

The shared `.tab.active` rule now uses the existing `--ink` token instead of
the light-theme `--accent` token. This one CSS declaration covers Now,
Portfolio, Schedule, Board, Cockpit, CMDB, Fleet, Economy, Models, Trust, and
Assistant without changing navigation markup, focus, hover, active background,
responsive behavior, authorization behavior, or data behavior.

## Qualification

- Focused tests: `6 passed in 1.52s`.
- Full suite: `464 passed, 6 warnings in 31.31s`.
- Ruff: `All checks passed!`.
- Node syntax: every `src/skdashboard/static/js/*.js` and `scripts/*.mjs` file
  passed `node --check`.
- Diff hygiene: `git diff --check` passed.
- Chrome 151 CDP: 11 surfaces, light and dark themes, and 1280, 390, and 320
  CSS pixel viewports produced 66 matrix entries and 744 computed link
  measurements.
- Minimum computed foreground/background contrast: `7.4735:1`.
- Old light active color mutation: `3.1184:1` on every surface, proving the
  qualifier detects the released defect.
- Browser side effects: 0 non-GET requests, 0 external requests, and 0 runtime
  exceptions.

## Acceptance and boundaries

All visible dashboard navigation links meet the 4.5:1 normal-text threshold in
the required matrix. The patch adds no dependency, route, API, state mutation,
credential, release, or deployment.

Rollback is the reversal of the single `.tab.active` foreground declaration.
That reversal is intentionally rejected by the old-color sensitivity check.

Known limitation: this card qualifies the 11 named live surfaces and every
navigation link visible within them. The Reliability destination introduced in
the current main line is measured where it is already visible, but adding that
destination to legacy surface markup remains outside this contrast-only card.
