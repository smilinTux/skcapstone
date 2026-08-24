# SKCP-20 unified scope qualification

Date: 2026-08-24

Card: `b7ada8b9`

Base main: `558e75ee32173ae578f88d810e906ee24fe4e2e8`

## Delivered boundary

The existing `/control-plane/now` workspace and protected
`GET /api/v1/overview` endpoint now share one bounded V1 scope contract. The
server validates the query before adapter construction and echoes the exact
normalized non-secret scope in the protected response. The browser renders
only when that response scope exactly matches the latest requested scope.

V1 truthfully supports the current estate projection, three presentation
roles, the latest source window, no baseline, all services, an optional silo
filter, an optional truth-state filter, and an optional opaque saved-view ID.
Unsupported, duplicate, empty, and oversize fields return a typed 400 before
any estate reader runs. `tenant_id` and `matter_id` return the same generic 403
before retrieval and never echo the supplied value.

The browser URL and local saved-view record use strict allowlists. A saved view
contains only its schema version, opaque generated ID, generated label,
creation and expiry times, route, normalized context, presentation filters,
and metric-registry version and hash. It contains no bearer, capability,
policy token, Tenant or Matter identifier, source data, watermark, evidence,
prompt, response, or authorization claim. Records expire after 24 hours and
are capped at eight. Missing, expired, wrong-schema, registry-incompatible, or
tampered records fail closed before the protected overview request.

Silo and truth filters apply to the estate table, data-quality issues, evidence
commands, visible context labels, and command search. Legacy unscoped tiles,
work, activity, and health are hidden while a filter is active. Each transition
clears the prior protected DOM before retrieval. Epoch binding prevents an
older response from rendering after a newer context request.

The native command dialog is available with Ctrl+K or Cmd+K. Its bounded
categories cover the authorized estate scope, visible metric definitions,
implemented workspaces, visible evidence, valid saved views, disabled reports,
and preview-only refresh actions. Arrow keys, Enter, Escape, Tab containment,
and trigger focus return are qualified. No command sends a mutation.

## Qualification

- Focused API, scope, Now, quality, fixture, and read tests: 29 passed.
- Full repository test suite: 398 passed with 145 warnings.
- Ruff over `src` and `tests`: passed.
- Node syntax for both browser modules and the CDP qualifier: passed.
- Real Chrome 151 CDP qualification: passed.
- Default projection: 12 silos from 16 bounded sources.
- Deliberately mismatched response scope: prior data was purged and the UI
  failed closed.
- Delayed portfolio response followed by flow: only the latest flow context
  rendered.
- Browser back, forward, refresh, saved restore, and share link: passed.
- Expired, tampered, and future-issued saved views: zero protected overview
  requests. The future-issued case preserves an exact 24-hour duration but
  shifts both timestamps to 2099, proving bounded issuance time is enforced.
- Unsafe, duplicate, protected, and oversize deep links: zero protected
  overview requests and no protected value retained in URL, DOM, or storage.
- Live 401 and live 403 after a previously authorized saved view: dialogs,
  evidence, quality details, green legacy claims, and protected source values
  were purged.
- Ctrl+K and Cmd+K accessibility tree and keyboard-only operation: passed.
- 390 px and 320 px layouts, reduced motion, and normal-text contrast: passed.
- Minimum checked contrast: 5.833:1.
- Non-GET requests: zero.
- External requests: zero.
- Browser runtime exceptions: zero.

## Deliberate limits

No authorized scope catalog exists yet for portfolio, project, product,
service, team, Tenant, Matter, node, environment, or measurement-lane detail.
Those selectors are not invented. Tenant and Matter remain unavailable until
an owner-policy projection authorizes them before retrieval.

Saved views are local browser presentation state, not durable governed reports
or authorization records. Reports remain visibly unavailable. The command
palette performs navigation, evidence opening, view selection, and existing
preview-only actions only. This slice adds no write route, report route,
framework, dependency, deployment, activation, dispatch, or external action.
