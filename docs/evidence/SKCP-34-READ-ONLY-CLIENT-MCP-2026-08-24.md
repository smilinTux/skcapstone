# SKCP-34 read-only client and MCP evidence

Card: `5858a34f`

## Delivered boundary

- `src/skdashboard/control_plane_client.py` discovers one canonical HTTPS
  origin, keeps the caller-owned bearer private, and exposes only the frozen
  overview, board, fleet, economy, report, insight, pagination, ETag, event,
  saved-scope, metric-family, and evidence-reference reads.
- Every successful JSON document is validated against exact packaged copies of
  the published V1.1 OpenAPI components and JSON Schemas. A response that does
  not match the frozen schema fails closed.
- `src/skdashboard/control_plane_fixture.py` provides a deterministic public
  synthetic ASGI server for client and agent development. It includes current
  metric evidence, an immutable report, pagination, ETag, event reset, and a
  typed model abstention without production owner state or live inference.
- `src/skdashboard/control_plane_mcp.py` publishes five fixed MCP resources and
  one exact report template. It registers no MCP tool or command handler and
  rejects arbitrary URIs, query strings, protected scope fields, credentials,
  raw content, and capability material.

## Acceptance evidence

1. Discovery requires the exact HTTPS well-known path and same-origin entry and
   health URLs. Cross-origin, redirected, malformed, or oversized discovery
   fails closed.
2. Overview, metric-family, saved-scope, report, insight, evidence, pagination,
   ETag, and event-resume behavior is asserted in
   `tests/test_control_plane_client.py`.
3. The raw synthetic API document and typed client document are exactly equal,
   including metric value, definition hash, scope, truth state, freshness,
   quality, and evidence references.
4. MCP resource allowlisting, protected-field rejection, report-template
   access, bearer exclusion, and absence of list-tools and call-tool handlers
   are asserted in `tests/test_control_plane_mcp.py`.
5. The built wheel contains the client, fixture, MCP module, exact contract
   copies, and `skdashboard-control-plane-mcp` console entry point. An isolated
   wheel install validated both report and insight schemas successfully.

## Qualification

- Focused client and MCP suite: `13 passed`.
- Protected read, report, insight, contract, fixture, decision-context, runtime,
  queue, and write-gate suite: `121 passed, 6 warnings`.
- Full repository suite: `514 passed, 8 warnings`.
- Ruff over `src/` and `tests/`: passed.
- Ruff format over changed Python: passed.
- Wheel build and isolated install smoke: passed.
- Existing real Chrome 151 AI fail-closed qualifier: passed with keyboard and
  focus return, 390 and 320 pixel layouts, delayed response purge, 401 and 403
  purge, stale-response rejection, and zero writes, external requests, or
  browser exceptions.

The eight warnings are existing `jsonschema.RefResolver` deprecation warnings.
The initial boundary run was blocked by another task's shared editable CapAuth
install. Qualification used a process-local import cleanup and the approved
installed CapAuth runtime without changing shared state.

## Non-authorizations and limitations

- No owner record, report snapshot, saved view, policy, workflow, or deployment
  state was created or changed.
- No MCP tool, action preview, command, approval, authorization, dispatch,
  shell, filesystem, browser, connector, individual ranking, protected Matter,
  or HammerTime access was added.
- Insight query is contract-bound but remains unavailable when the deployed
  runtime does not serve the frozen route.
- Older live overview responses can contain scope fields outside the frozen
  V1.1 schema. The client reports schema failure rather than silently adapting
  or weakening the contract.
- The MCP launcher requires a caller-provisioned mode `0600` bearer file. This
  card does not create credentials or broaden CapAuth policy.

## Rollback

Revert the SKCP-34 merge. The change adds only package code, packaged contract
copies, tests, documentation, and one console entry point. It performs no data
migration and creates no production state, so rollback requires no owner data
or report-snapshot deletion.
