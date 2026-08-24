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
All notable changes to SKDashboard are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Accepted only the canonical base64url CapAuth bearer transport at the
  control-plane PEP while retaining strict malformed-token denial.

- Added a fail-closed cross-estate data-quality projection and accessible
  overview strip with explicit truth-state coverage, safe provenance, and
  non-dispatching refresh previews.

- Added a public-synthetic full-estate fixture pack with deterministic metric,
  UI, API, agent, MCP, report, forecast, and failure qualification profiles.

- Added a versioned, hash-addressed control-plane metric registry and
  deterministic synthetic calculations spanning every approved estate family,
  truth state, and measurement kind without individual productivity ranking.

- Enforced CapAuth and exact named browser origins across protected `/api/v1`
  reads and events, and added a bounded authenticated `/metrics` projection.

- Added bounded, read-only cross-estate observation adapters with explicit
  owners, populations, query budgets, watermarks, classifications, freshness,
  coverage, and fail-closed unavailable states.

- Added an independent PASS R5 review of the F13 fail-closed preview repair,
  approval authority projection, real Chrome accessibility behavior, and all
  29 forward dependency gates while preserving pre-gate topology drift.

- Added frozen `/api/v1` read-only health, overview, board, fleet, Economy, and
  event projections with bounded pagination, ETags, explicit partial states,
  and reconnect-safe server-sent events.

- Added an independent FAIL R3 review after an unknown authorization-preview
  URL state rendered ready and false V2/H5/R4 provenance contradicted H4.

- Added an independent PASS review of the corrected V1.1.3 source receipt v2,
  its exact attestation, rejected v1 history, and remaining deployment gates.

- Recorded the human owner's exact-hash attestation of the corrected V1.1.3
  approval source receipt v2 and continued rejection of v1.

- Added a corrected v2 approval source receipt containing the exact extended
  V1.1.3 approval and preserving the human rejection of v1.

- Added an H4 attestation record for the v1 approval source receipt and its
  narrow independent-review scope.

- Added an independent FAIL review after a concurrent machine-readable source
  receipt introduced a different, unattested approval transcription.

- Added a machine-readable exact-source approval receipt with explicit
  normalization, unknown metadata, and append-only attribution supersession.

- Added an append-only supersession that preserves the exact current V1.1.3
  approval and resolves the contradictory PR #37 attribution claim.

- Added an independent FAIL rereview of the exact V1.1.3 lineage candidate
  after a concurrent approval-attribution record created contradictory claims.

- Added an append-only approval attribution correction that preserves the
  human owner's exact V1.1.3 approval wording.

- Recorded the human owner's exact-hash approval of the V1.1.3 lineage
  correction candidate while preserving all deployment and action gates.

- Added an append-only V1.1.3 lineage correction candidate that truthfully
  records the retained historical PNG bytes without changing approved history.

- Added an append-only V1.1.2 schedule supplement and V2.1 wireframe that keep
  policy visibility separate from source truth and make closed dialogs inert.

- Recorded the human owner's exact-hash approval of the V1.1.2 control-plane
  candidate and its dependency-gated implementation authorization.

- Added the truthful V1.1.2 control-plane review candidate, reproducible
  CardStore capture, preserved lineage artifacts, and detached manifest seal.
- Scoped gitleaks CI to the complete history reachable from checked-out HEAD,
  preventing unrelated retained remote refs from contaminating candidate CI.

- Added the human-approved SK control-plane architecture candidate, versioned
  metric and report contracts, `/api/v1` OpenAPI baseline, breadth-first sprint
  plan, synthetic wireframes, exact-hash approval record, and executable
  contract tests.
- Added an independent review report that keeps implementation blocked on
  false-zero truth-state, empty recommendation-grounding, and empty
  approval-requirement contract gaps.

- Added a governed SKCounter AI Usage workspace beside Autopilot and Joule,
  including separate harness and gateway measurement lanes, token and cost
  trends, model and fleet breakdowns, collector freshness, coverage gaps, and
  strict rejection of raw harness content.

- Added a dedicated Syncthing CMDB summary with fleet health, pending work,
  error totals, peer connectivity, and per-node service state.

- Added CMDB operator coverage by node and collector, verified reconciliation
  history, evidence freshness, provenance, last-seen and health history, grouped
  relationship impact, linked ITIL records, and filters for type, node, status,
  owner, tag, staleness, and source. Discovery remains preview-first, and both
  plan and apply responses now expose authorization and execution state.

- Added CMDB status, plan, drift, and capability-gated apply APIs plus explicit
  Plan/Apply controls in the CMDB view. The former seed operation remains only
  as a versioned compatibility alias over the canonical reconciler.

- Honor `SKDASHBOARD_HOST`, `SKCAPSTONE_DAEMON_URL`, and
  `SKGATEWAY_ADMIN_URL` in long-running dashboard deployments, while retaining
  loopback defaults and stripping the inference-only `/v1` suffix from the
  fallback gateway management origin.

- Reconcile dashboard ITIL shadow cards with authoritative lifecycle state,
  cancel queued work when a record becomes terminal, and reject new runs for
  closed or otherwise terminal records.

- Added bounded, read-only `GET /api/cmdb/search?q=...&limit=...` and an
  accessible CMDB search form. Search covers CI identity, type, state, node,
  ownership, description, tags, and attributes; result details continue to use
  the canonical exact-CI endpoint.

- Bounded dashboard shutdown with Uvicorn's supported `SIGINT` path,
  10-second graceful-drain limit, and a repo-owned 15-second systemd
  `TimeoutStopSec` drop-in, preventing active browser streams from turning
  every deployment restart into a 90-second SIGKILL.
- Documented the deployed read-only ATLAS Operator Cockpit contract and its
  fail-closed treatment of missing freeze and malformed operational evidence.

### Documentation

- Recorded the tested PR #16 CAB rollout and GitHub-first deployment procedure in
  the SOP so the live service is updated from a traceable repository commit.

### Fixed

- Failed closed unknown, missing, blank, and whitespace authorization-preview
  URL states, and added an append-only approval authority projection that
  preserves V1 and H4 while classifying unsupported V2, H5, and R4 claims.

- Hardened the V1.1 control-plane contracts with distinct unreachable truth,
  nonempty evidence and watermark requirements, nested insight grounding, and
  ready mutating preview target and version invariants. Proposed preview-action
  next steps now require nonempty target, action contract, and parameter
  proposal references, with fail-closed composed insight and recommendation
  outcomes.

- Pinned the CI Ruff version so an unrelated upstream release cannot change the
  lint gate without a reviewed repository change.

- Restored the missing `main` push trigger in `publish.yml`; without it, the
  documented automatic patch-tag job was unreachable except by manual dispatch.

### Added

- Human CAB controls in the Operator Cockpit now provide dedicated Approve,
  Reject, and Abstain actions. `POST /api/change/{id}/cab-vote` requires both
  the `change.cab_vote` capability and a verified CapAuth operator session;
  `X-SK-Actor` alone cannot cast a human vote. The verified device identity is
  retained in the append-only consent record and vote audit conditions.
- Approved changes now expose scheduling controls with ASAP, tonight 10 PM to
  2 AM, tomorrow 10 PM to 2 AM, and editable local-time windows. Scheduled
  changes expose a separate deployment arm action. The AI panel on a change is
  labeled as draft preparation and no longer offers execute mode.
- Regression coverage for missing and invalid operator sessions, capability
  denial, approve/reject/abstain folding, lifecycle conflicts, unknown changes,
  consent attribution, and the human no-self-approval guard (incident
  `inc-6cf27bda`, coordination card `adab5895`).

- `start_dashboard()` now accepts an explicit bind address for the SKCapstone
  dashboard CLI's `--host` option. Loopback remains the secure default.

### Security

- **Three write routes no longer bypass the authorization gate** (coord card
  `9d37d53d`). `POST /api/card/{id}/{action}` (every board mutation),
  `POST /api/cmdb/seed`, and `POST /api/models/advertise` reached the coordination
  store, the CMDB, and skgateway's advertise allowlist without ever consulting
  `queue_authz`. Their only control was the `127.0.0.1` bind. All three now go through
  the same staged token/pdp/both path as the queue and `change.*` routes.
  - Capabilities: `agentrun.queue` (interim) for the two coordination-store writes,
    `skgateway.admin` for the gateway allowlist write. See `SECURITY.md` for why the
    first two are a deliberate reuse of an already-seeded capauth row rather than a new
    ungranted one.
  - **No behavior change on the deployed seat.** The dashboard runs with neither
    `SKAI_AUTHZ` nor `SKAI_QUEUE_TOKEN` set, so the gate is still loopback-open and
    these routes still answer exactly as before. What changed is that setting either
    variable now arms them too.
  - This does **not** fix `X-SK-Actor` being self-asserted rather than authenticated.
    That belongs to the Unified Consent Plane epic; `SECURITY.md` says so plainly.

- **Clients now send `x-sk-capability`** (Unified Consent Plane P1.3, coord card
  `a638b490`). `api.js`, `assistant.js`, `ai_compose.js`, `cmdb.js`, and
  `models.html` all attach it, via a new shared `authHeaders()` helper sourced from
  a new `GET /api/auth/capability` route. Before this, `SKAI_AUTHZ` could not be
  flipped without breaking every button: the token path denied on a missing header
  and the PDP path denied because no client ever authenticated the caller.
  - **What actually authenticates the handout: nothing yet.** `/api/auth/capability`
    is loopback-trust only, exactly like every other route on this dashboard - it
    hands back `SKAI_QUEUE_TOKEN` and `SKAI_OPERATOR_ACTOR` verbatim to anyone who
    can reach the port. It is the seam a verified operator session
    (`x-operator-token` / `capauth.pairing.verify_operator_session`) will gate once
    that is wired end to end here; that wiring is a separate, later card.
  - The hardcoded `X-SK-Actor: "operator"` literal is gone from every client.
    `"operator"` is not an enrolled capauth subject and would have failed every PDP
    check; the new default on an unconfigured seat is the fleet's `"unattributed"`
    convention for an identity that cannot be backed.

### Fixed

- `docs-check` (tier 3) has been **red on `main` since the consent PR** merged: `SOP.md`
  claimed skdashboard has no `skcapstone.card_store` imports, and `consent.py`
  introduced one. The SOP now documents `consent.py` as the single sanctioned exception
  and the evidence check pins it to exactly that file, so a second importer still fails
  the gate. The gate was working; nobody read it.

### Added
- **Fleet install-profile drift panel and alert** (`/fleet`, skcapstone epic `3bbf39ea`,
  card `d1c6d605`). Shows, per node, which units and packages disagree with the install
  profile the node is bound to.
  - The grading is NOT reimplemented here. `collect_drift()` calls the fleet CLI's own
    grader, so the panel and `skfleet node doctor --all` cannot disagree about who gets
    graded or how.
  - Severity stays split three ways all the way to the pixels. `forbidden` is the only
    error grade, because it is the only one meaning a node is doing something it was
    told not to; `missing_required` is warn (a node mid-install) and `unexpected` is
    info (usually a manifest lagging reality). Flattening those into one badge is how a
    signal becomes wallpaper.
  - A node with no role, no profile, or no published inventory renders as an explicit
    SKIPPED card carrying its reason, and holds no severity key at all, so it can never
    be summed as clean. An absent inventory previously read as "everything is missing"
    and graded healthy nodes as drifted.
  - The alert fires on error-grade findings only, edge-triggered on a fingerprint with a
    300s floor, and persists its state so a dashboard restart is not an alert. A failed
    send retries once per cooldown rather than every poll, and a cleared condition
    records silently so a recurrence still fires.
### Added

- `tests/test_write_route_gates.py`: the three routes above prove
  deny-without-capability, allow-with-capability, and unchanged loopback-open behavior,
  plus a sweep of the real route table asserting **every** registered `POST` endpoint
  calls a gate. A future ungated write route fails the build instead of shipping.
- The seven documents `SK_REPO_DOC_STANDARD` requires: `SOP.md`, `SECURITY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, and this file. `README.md` and `LICENSE`
  already existed and were not relicensed or rewritten.
- `.github/workflows/docs-check.yml`, the `DOCS_FRESHNESS_STANDARD` gate (tiers 1 and 2
  to start). `SOP.md` carries a `docs-evidence` block of 8 executable checks so tier 3
  can be enabled once the gate has run clean.

### Changed

- One gate body, three names. `_capability_gate` now holds the staged authz decision
  (the loopback-open carve-out plus the `queue_authz.authorize_capability` call);
  `_queue_gate` (mode-derived `agentrun.*`) and `_change_gate` (explicit `change.*`) are
  thin wrappers over it, and `_change_deny` is an alias of the shared `_gate_deny`.
  Behavior is identical; there is now one place to read instead of two copies to keep in
  sync.
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
- **Unified Consent Plane, Phase 2: `consent.granted` events (coord card `90d23f56`).**
  capauth's PDP has always emitted an AUDIT obligation on every `decide()` call, and
  every PEP discarded it - there was nowhere in the fleet to ask "did a human consent
  to this action, and when." `queue_authz._default_decide_fn` now returns the PDP's
  `Decision.obligations` alongside `allow` instead of collapsing to a bare bool, and
  `authorize_capability`/`authorize_queue` surface it as an `"obligations"` key on
  their result dict.
- New `skdashboard.consent` module: builds a `consent.granted` Signed Provenance
  Envelope (`spe1`, per `PROVENANCE_AND_MUTATION_STANDARD.md`) and persists it into
  the object's OWN append-only event store - no new store. The dashboard queue-AI PEP
  (`_queue_run`) writes into the CardStore (`skcapstone.card_store.CardStore.append_event`);
  the change-management validate/schedule/arm PEPs write into the ITIL changes store
  (`skcoord.itil.ITILManager._append_event`). The consenting subject prefers a
  capauth-verified operator session (`x-operator-token`, `capauth.pairing.
  verify_operator_session`) over the self-asserted `X-SK-Actor` header, which keeps
  working unchanged for every other actor-facing field; an unresolved identity is
  recorded as the literal `"unattributed"`, never a synthesized placeholder. Signing
  is permissive (sign when a capauth key is present, `sig.value` null when not),
  matching the SPE P2 posture.
- New `GET /api/card/{card_id}/consent` and `GET /api/change/{id}/consent` routes:
  answer "who consented to this action, and when" straight from the object's own
  event log.

### Fixed

- Nothing in the fleet persisted the PDP's audit obligation before this change; every
  gate decision was allow/deny with no durable trace of which authenticated subject
  consented.
