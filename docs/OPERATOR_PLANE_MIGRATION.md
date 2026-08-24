# Operator-plane migration: spec.cli to skoperator.remote/v1

Status: RATIFIED 2026-08-23 (P0-P3 implemented; see CHANGELOG). Standard: [OPERATOR_PLANE_REMOTE_STANDARD.md](OPERATOR_PLANE_REMOTE_STANDARD.md).
Principle: no flag day. Every phase is additive, gated, and reversible; the
old lane keeps serving until the new lane proves parity per app.

## 0. Where the 8 registrations stand today (Eyes, PR #178)

| App | cli lane | seat lane | Verdict |
|---|---|---|---|
| cmdb | works | works | OK (baseline candidate) |
| skchat | works | works, 1 conflict (AuthEnforced) | CONFLICT |
| skcode | works | 4 conflicts | CONFLICT |
| skcomms | works | conflicts in OPPOSITE directions (PathHealthy, QueueDrained) | CONFLICT |
| skgateway | DEAD (binary not on PATH) | built-in only, FIRING UpstreamServing | one-eyed |
| skmemory | works | 2 conflicts (EmbedServing, ReconcileFresh) | CONFLICT |
| skos | works | 1 conflict (SchedulerAlive) | CONFLICT |
| skdashboard | exits 2 | none | BLIND |
| fleet | not registered | seat-only built-in | invisible to discovery |
| skbrain | none (disabled shell module) | none | invisible to everyone |

## Phase 0: truth reconciliation (BLOCKS everything else)

Card 504d0046. For each of the 10 conflicting conditions: determine which lane
is lying, fix that lane, land a regression test in the owning repo. Exit gate:
one full eyes pass with CONFLICT count 0.

Why first: the remote standard designates the app's own facet as the single
authoritative producer. Promoting a lane that is currently WRONG on live
conditions launders a lie into the source of truth. Cheap, high-value, needs
no new transport.

## Phase 1: node agent HTTP surface (additive, gated OFF)

- Extend `sknoded` with the `/operator/v1` listener per the standard: tailnet
  bind only, port 9392, capauth request verification, healthz/readyz, observe
  + watch, envelope signing. Gate: `SKOPERATOR_HTTP` env, default off (same
  pattern as `SKOPERATOR_MANIFEST_DISCOVERY`).
- It serves exactly what the node can already produce: exec of the LOCAL
  `spec.cli` (where PATH is finally evaluated on the right node) and the
  in-process adapters, tagged with provenance.
- Register 9392 in PORTS.md and `FLEET_RESERVED_PORTS` in the same PR.
- Carry the Eyes read-only test to the HTTP path (byte-identical fleet tree).
- Exit gate: `curl` over tailnet from .41 to noroc2027 returns signed
  envelopes for all locally-homed apps; unauthorized key gets 403; missing
  endpoint renders Unknown (NoEndpoint) in eyes.

## Phase 2: registration contract v2 (schema only)

- `Operatorapp.spec.contractVersion: 2` adds `endpoint`, `node`, `transport`.
  Store-side validation only; v1 specs stay valid and mean cli-local.
- Seat, eyes, and discovery learn the precedence order:
  1. `endpoint` (authoritative, remote-capable)
  2. `spec.cli` executed by the app's HOME-NODE agent only (local fallback)
  3. seat built-in adapter (advisory only, conflict detection)
  A remote seat NEVER execs `spec.cli` itself again; that path is deleted from
  the seat's remote view in this phase (it was already dead in production).

## Phase 3: per-app cutover (one app at a time, worst-first)

Each app follows the same recipe: register endpoint (v2 spec) -> dual-read for
one week (endpoint authoritative, old lane advisory) -> zero LaneConflict and
zero Unknown-regression -> demote old lane. Order and app-specific notes:

1. **skdashboard** (BLIND today; any working lane is strictly new signal).
   Build a real facet, serve it via the noroc2027 node agent. Its declared
   `skcapstone dashboard operator` cli either becomes real or the spec drops
   the cli field.
2. **fleet** (seat-only today). Register an Operatorapp v2 pointing at the
   control-plane node agent itself (the agent IS the fleet facet). Discovery
   finally sees it; the 12 firing MissedRun cronjobs become estate-visible.
3. **skbrain**. Register with an explicit disabled marker so it renders as
   `Unknown (NoEndpoint)` instead of not existing. Invisible and disabled are
   different states; today they are collapsed.
4. **skgateway** (dead cli, one-eyed, FIRING). Self-serve the facet on the
   daemon it already runs (`:18780/operator/v1/...`), register the endpoint.
   This fixes the dead-cli problem WITHOUT waiting for the node move, and
   makes the later move a one-field endpoint update (section: skgateway to
   .100 below).
5. **cmdb** (cleanest lanes) as the low-risk template for the exec-shape
   cutover, then **skchat, skcomms, skmemory, skos, skcode** in that order
   (post-phase-0 they are conflict-free; order is by blast radius, comms
   before memory before code).

## Phase 4: skgateway to 192.168.0.100 (HARD-GATED on the disk SEV2)

.100 is at 98% disk (5.3G free of 195G) with an open SEV2 naming it an
imminent outage vector. Sequencing is not negotiable:

1. Close the SEV2 first: identify the consumers (models are the likely bulk),
   offload/prune to a floor of >= 20G free and >= 10% headroom. The NFS work
   already done on .100 is the natural relief valve.
2. Add `DiskPressure` to the node conditions the .100 node agent serves
   (threshold: firing under 10% or under 15G free). This makes the gate
   machine-checked, not a memory.
3. Stand up the .100 node agent (phase 1 code, gate on) and verify it serves
   healthz/readyz over tailnet.
4. Move skgateway. Because phase 3.4 already put its facet on its own daemon,
   the operator-plane change is ONE field: the registered endpoint URL. The
   move admits only while .100 `DiskPressure=False`.
5. Dual-read week: old-node registration advisory, new-node authoritative.

## Phase 5: deprecation end-state for `spec.cli`

- `spec.cli` is KEPT in the schema, deprecated for new registrations, and
  redefined: a node-LOCAL fallback the home-node agent may exec when an app
  has no endpoint. Remote semantics are removed permanently.
- Seat built-in adapters: advisory through each app's dual-read window, then
  DELETED per app after 2 clean weeks of endpoint parity (no conflicts, no
  Unknown regression). Keeping them longer recreates the two-lane lie factory
  this migration exists to end.
- Eyes remains the migration instrument throughout: its CONFLICT verdict is
  the per-app cutover gate, and its "blind even if unfrozen" list must be
  empty at end-state (skdashboard, fleet, skbrain included).

## Rollback

Every phase is a gate flip or a spec field. Rollback at any point: clear the
`endpoint` field (v2 spec reverts to cli-local semantics) or drop the
`SKOPERATOR_HTTP` gate. No phase deletes the old lane before its replacement
has a clean dual-read window, so rollback never enters a blind state worse
than today's.

## What ATLAS's freeze means here

Nothing in phases 0-5 requires unfreezing. Every gate is observation parity,
verified through eyes (freeze-independent by construction). The act path is
untouched: same ratification, same freeze check, now enforced server-side at
the app's home node.
