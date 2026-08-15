# Changelog

All notable changes to `skdashboard` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are **not** written in the tree: `setuptools_scm` derives them from the git
tag, and `.github/workflows/publish.yml` cuts the next patch tag on every push to
`main`. So a heading here names a tag that exists, and the tag is the source of truth.

> This file was added on 2026-08-14, after the fact. Entries below `[Unreleased]` were
> reconstructed from the git tags and their commit subjects, so they record **what
> shipped in each tag**, not a contemporaneous author's notes. Anything that was not
> visible in the history is absent rather than guessed. `git log v0.1.7..v0.1.8` remains
> the authoritative diff for any release.

## [Unreleased]

### Added

- The seven documents `SK_REPO_DOC_STANDARD` requires: `SOP.md`, `SECURITY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and this file. `README.md` and `LICENSE`
  already existed and were not relicensed or rewritten.
- `.github/workflows/docs-check.yml`, the `DOCS_FRESHNESS_STANDARD` gate (tiers 1 and 2
  to start). `SOP.md` carries a `docs-evidence` block of 8 executable checks so tier 3
  can be enabled once the gate has run clean.

### Changed

- `README.md` now states the maturity tier and links the rest of the document set, as
  the doc standard requires of the first lines.

### Notes (documented, not changed)

- The service still has **no console script and no unit of its own**. It is launched as
  `skcapstone dashboard --port 7778` through an alias shim that lives in `skcapstone`.
  See `SOP.md` section 5.
- The privileged-action gate still ships **loopback-open** while neither `SKAI_AUTHZ`
  nor `SKAI_QUEUE_TOKEN` is set. See `SECURITY.md`.

## [0.1.8] - 2026-08-13

### Added

- Change-management PEP routes `POST /api/change/{id}/{validate,schedule,arm}` plus the
  matching kanban chips (CM P2.3, P2.4). These are the first routes gated on an explicit
  `change.*` capability rather than one derived from an agentrun mode, which is why
  `queue_authz.authorize_capability` was generalized out of `authorize_queue`.

## [0.1.7] - 2026-08-13

### Fixed

- Nav icons survived the embed href rewrite by keying on `data-nav`.

## [0.1.6] - 2026-08-13

### Added

- Fleet-wide Economy view at `/economy` and `GET /api/economy`: autopilot cost ledger
  plus skjoule wealth, both lazy-imported so a seat without `skharness` or `skcapstone`
  gets a well-formed empty payload instead of a 500.

## [0.1.5] - 2026-08-12

### Fixed

- **Security.** The assistant surface could queue an execute-tier run behind a
  propose-tier capability check (R3). The assistant now goes through the same coarse
  gate as the rest of the privileged surface.

## [0.1.4] - 2026-08-12

### Added

- Generalized fleet routes `GET /api/suggest/{surface}/{id}` and
  `POST /api/queue/{surface}/{id}`, backed by the new `surface_registry.py` (a pure,
  stdlib-only ItemRef to shadow-card-id mapping) and the staged `SKAI_AUTHZ`
  token/pdp/both authorization in `queue_authz.py` (P2.2, P2.3).

## [0.1.3] - 2026-08-12

### Added

- `GET /api/gtd` for the native app (P3.4).
- A Cards (model dex) view in the models console.

## [0.1.2] - 2026-08-12

- Merge-point release, no distinct feature commit of its own.

## [0.1.1] - 2026-08-08

### Fixed

- **Release.** The tag job ranked tags by version (`sort -V`) instead of walking
  ancestry. `git describe` only sees tags that are ancestors of HEAD, so a stranded tag
  made it report "previous: none" and restart the sequence, which is how 0.0.1 and 0.0.2
  came to be published *below* the existing 0.1.0. A release must never go backwards.

## [0.1.0] - 2026-08-06

### Added

- PyPI Trusted Publishing (OIDC) workflow, `v*` tag triggered, no token.

## [0.0.2] - 2026-08-08

### Fixed

- The build now pins the version to the tag the job just cut, instead of rebuilding a
  stale one.

## [0.0.1] - 2026-08-08

### Added

- Automatic publish to PyPI on push to `main`.

> 0.0.1 and 0.0.2 sort **below** 0.1.0 and were published out of order by the tag bug
> fixed in 0.1.1. They are historical artifacts, not a supported line.

## Pre-tag history - 2026-08-06

### Added

- **Initial extraction (CR-4.3).** The operator dashboard (`:7778`) was split out of
  `skcapstone` into this package: the Starlette app, the kanban / ITIL / CMDB / overview
  / assistant modules, and all static assets. `skcapstone.dashboard` became a
  transparent alias shim so the CLI, its inbound call sites, and the tests resolve to
  this implementation byte-identically.
- CI (CR-4.6): blocking `lint` (ruff), `test`, and `build` jobs, plus a light
  self-contained smoke suite.

### Changed

- `trust_graph` is imported from `capauth.trust` directly (CR-3.6 shim retirement).
