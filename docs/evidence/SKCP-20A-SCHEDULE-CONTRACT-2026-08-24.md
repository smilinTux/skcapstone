# SKCP-20A schedule contract freeze

Date: 2026-08-24

Card: `c3a9c9e9`

Base main: `b0c9218bdc43c73a462c6f70e31a44a5759dd69a`

## Frozen contract

The additive `docs/contracts/schedule/v1.0.0/` set freezes:

- one versioned, hash-bound schedule projection envelope;
- typed outcomes, projects, epics, releases, milestones, work packages, teams,
  services, architecture migrations, and ITIL change windows;
- separate baseline, planned, and actual date values;
- UTC instants with a required named display timezone;
- explicit null-date, baseline-variance, rollup, and partial-rollup states;
- evidence-bearing dependency type, direction, lag, blocker, and cycle states;
- policy-filtered ITIL, blackout, migration, and deprecation overlays;
- fail-closed cycle and critical-path outcomes;
- immutable no-write scenarios with stable input, diff, and reset identity; and
- non-executing reschedule previews bound to an exact projection version and
  base SHA-256.

The projection requires `individual_ranking_prohibited: true`. No person-level
activity, cards, tokens, commits, cost, Joules, or productivity ranking field is
included.

## Qualification

- Focused schedule contract tests: 6 passed with 2 deprecation warnings.
- Full repository suite: 426 passed with 6 deprecation warnings.
- Ruff over `src` and `tests`: passed.
- Ruff format check for the new contract tests: passed.
- Draft 2020-12 schema checks for all three schemas: passed.
- JSON syntax checks for all four contract documents: passed.
- Git whitespace check: passed.

## Limits and rollback

This card is contract-only. It adds no runtime route, producer, UI, deployment,
protected-data retrieval, scenario mutation of owner records, reschedule
authorization, external action, or HammerTime access. SKCP-21A remains a
separate implementation card.

Rollback is reverting the additive contract commit before a producer adopts
it. This card creates no migration or runtime state.
