# SKCP-25 governance metric-lineage and data-quality qualification

Date: 2026-08-24

Card: `b548a77a`

Base main: `b3b49f85ccfdd785550b7918bd131e831686f2a6`

## Delivered slice

The protected read-only `/control-plane/governance` workspace projects existing
metric-registry, approved aggregate, data-quality, CapAuth, and SKCoord
CardStore evidence through the typed CapAuth and request-local currentness
boundary. It includes:

- every registered metric's definition version and hash, authoritative owner,
  adapter, classification, calculation, source watermark, source truth state,
  and explicit human-review state;
- all 16 declared source owners with classification, visibility, age, TTL,
  coverage, watermark, and bounded safe errors;
- policy availability and aggregate denial counts kept as separate states;
- separate stale-evidence, partial-coverage, orphan-dependency,
  duplicate-card, missing-criteria, and claim-TTL truth classes;
- evidence, owner, severity, due state, and preview-only remediation metadata;
  and
- attributable correction and supersession event references projected from
  append-only CardStore events without event payload content.

A semantic-duplicate registry, claim-expiry contract, metric-specific human
review registry, access-review aggregate, and immutable report-snapshot owner
reader are not configured. Those states remain Unknown with `null` values. They
are not rendered as zero, healthy, reviewed, or current.

## Qualification

- Focused governance tests: 6 passed.
- Changed protected-boundary and navigation tests: 71 passed.
- Full repository suite before browser qualification: 485 passed with 6 existing
  deprecation warnings.
- Ruff over `src/` and `tests/`: passed.
- JavaScript syntax checks over all static and qualification scripts: passed.
- Chrome 151 CDP qualification: 13 metric definitions, 16 declared sources,
  eight distinct required finding classes, append-only history, keyboard role
  change, 390 px and 320 px layouts, zero writes, external requests, or browser
  exceptions.

## Acceptance evidence

1. Metric lineage includes definition, owner, source watermark,
   classification, calculation, and explicit human review state.
2. Policy unavailable, denial, stale evidence, partial coverage, orphan
   dependency, duplicate card, missing criteria, and claim-TTL states use
   separate typed entries. Unknown classes have `null` values.
3. Correction and supersession history emits event identity, target,
   attribution, time, and append-only evidence reference. It excludes mutable
   payloads and never rewrites source history.
4. Protected metric classifications carry the request decision reference. Raw
   Card titles, descriptions, comments, criteria payloads, audit payloads,
   denial details, secrets, capabilities, Tenant data, Matter data, and
   HammerTime material are excluded.
5. Every emitted finding has owner, severity, due state, evidence, and a
   preview-only remediation object with dispatch explicitly unauthorized.

## Limits and rollback

This slice does not mutate owner records, correct data, approve work, dispatch,
deploy, rank individuals, access protected Matter content, or access
HammerTime. It does not add a command or action API. Report snapshots,
metric-specific reviews, semantic duplicates, access reviews, and claim expiry
remain Unknown until approved owners expose bounded contracts.

Rollback is reverting the implementation commit. No migration, owner-record
mutation, runtime deployment, or external effect is created by this card.
