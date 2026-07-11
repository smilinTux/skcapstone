# skcot: extract CoT/TAK out of skcomms core into a standalone package

Status: approved design (2026-07-11)
Owner: opus
Epic: coord `7a97fcb3`
Related: `docs/design/skchat-log-and-comms-syncthing-remediation.md` (the beacon-flood incident that motivated this)

## Problem

The CoT/TAK subsystem lives inside skcomms core. That coupling is what let a domain
protocol (position beacons) reach into the core federation send path and flood a human
agent's durable mailbox with ~270k throwaway envelopes. The root issue is a QoS mismatch:
skcomms core is built for reliable, signed, durable, ack'd mail; CoT position beacons are
fire-and-forget, supersede-only, stale-in-5-minutes situational data. They do not belong in
the same pipe.

## Key finding that makes this an extraction, not an untangling

CoT is already a clean leaf. Nothing in core skcomms (router, core, api, transports) imports
the `cot_*` or `geo` modules (verified by grep). The dependency is one-way: the CoT code
consumes skcomms core (`send_federated`, `Envelope`, `discovery`, `SKComms`); core knows
nothing about CoT. So extraction is moving a leaf out, not breaking a cycle.

## Design

### 1. Packaging & location
- New repo `~/clawd/skcapstone-repos/skcot`, Python package `skcot`, remote
  `github.com/smilinTux/skcot`, installed into `~/.skenv`.
- `install_requires = ["skcomms"]`; `extras_require = {"tak": ["takproto"]}`.
- skcomms MUST NOT import skcot (the leaf direction stays one-way). No compat shim in
  skcomms (that would reverse the dependency). Callers are updated instead.

### 2. Module moves (drop the `cot_` prefix inside the package)
| From (skcomms) | To (skcot) |
|---|---|
| `cot.py` | `skcot/codec.py` (CoT XML <-> Envelope, `is_ephemeral_beacon`) |
| `cot_server.py` | `skcot/server.py` (incl. `federation_ingest`, `_cot_peer_fqids`) |
| `cot_service.py` | `skcot/service.py` |
| `cot_agent.py` | `skcot/agent.py` |
| `cot_client.py` | `skcot/client.py` |
| `cot_pki.py` | `skcot/pki.py` |
| `geo.py` | `skcot/geo.py` (GEO_STORE situational picture) |

Stays in skcomms (generic, already merged to main 2026-07-11): `send_federated(ttl,
ack_requested)`, envelope `created_at` propagation, `PeerInfo.capabilities`, `'*'` broadcast
routing. These are not CoT-specific and other code may use them.

### 3. Ephemeral situational rail (by composition, not a new parallel path)
A position beacon must never become a durable mailbox file on any node. Achieved by composing
features that already exist after the 2026-07-11 remediation:
- Send side: beacons federate with short TTL (<= CoT `stale`, default 300s) + `ack_requested=
  False` + per-`(peer,uid)` supersede key. (Generic skcomms `send_federated`.)
- Peer gate: beacons only federate to `cot`-capable peers (see 4). Non-CoT peers (a human like
  `chef`) never receive PLI.
- Receive side: skcot registers a consumer for inbound `application/cot+xml` envelopes that
  writes them into `GEO_STORE` (the situational picture) and deletes them immediately
  (the delete-on-consume shipped in skcapstone `consciousness_loop`/housekeeping). No durable
  retention.

No new durable-bypass mechanism is introduced; the "no durable mailbox" property is an
emergent result of short-TTL + peer-gate + consume-to-GEO_STORE-and-delete.

### 4. Capability advertising (self-configuring gate)
When skcot runs on a node, it advertises `cot` in that node's `PeerInfo.capabilities` (via the
peer publication path in skcomms discovery). Peers then auto-discover CoT-capable peers, so the
`_cot_peer_fqids` gate configures itself instead of relying on a manual `SKCOMMS_COT_PEERS`
allowlist. This closes the review gap where nothing populated `capabilities`. The env allowlist
and `SKCOMMS_COT_STRICT` remain as overrides.

### 5. Migration & backward compatibility
- New systemd units `skcot-service.service` + `skcot-agent@.service`, replacing
  `skcomms-cot.service` / `skcomms-cot-agent@.service` (already stopped+disabled 2026-07-11).
- Entry points move: `python -m skcomms.cot_service` -> `python -m skcot.service`;
  `python -m skcomms.cot_agent` -> `python -m skcot.agent`.
- A `MIGRATION.md` documents the unit rename and any env-var changes.
- Old units stay disabled; nothing auto-starts until a mission is deployed.
- No import shim in skcomms. The only external callers are the systemd units (updated here)
  and the CoT tests (which move to skcot).

### 6. Testing
- Move the CoT-specific tests from skcomms to skcot (`test_cot_*`,
  `test_cot_beacon_*`, `test_cot_beacon_ephemeral_routing`, `test_cot_beacon_default_delivery`,
  `test_http_s2s_star_recipient` stays in skcomms if it is generic-routing).
- New skcot tests for the three properties that matter:
  1. capability auto-advertise: a running skcot node publishes `cot` in its PeerInfo.
  2. beacon-never-persists-durably: an inbound CoT beacon lands in GEO_STORE and no durable
     inbox file remains (the whole point of the extraction).
  3. codec round-trip: CoT XML <-> Envelope <-> CoT XML fidelity, incl. protobuf via takproto.
- Generic router / send_federated / `'*'` routing tests stay in skcomms and must remain green.

## Implementation phases (for the plan)
1. Scaffold the `skcot` package/repo (pyproject, package skeleton, CI, install into ~/.skenv).
2. Move the 7 modules + rename, fix imports (they import skcomms, not each other by old name).
3. Move + adapt the CoT tests; add the 3 new property tests.
4. Wire capability auto-advertise into skcot startup.
5. New systemd units + MIGRATION.md; update the disabled unit references.
6. Remove the moved modules from skcomms; confirm skcomms suite stays green with no dangling
   CoT imports; confirm no skcomms->skcot dependency.
7. Verify: fresh `~/.skenv` install of skcot imports, `python -m skcot.service --help`,
   the 3 property tests, and the full skcomms suite.

## Risks / notes
- The 2026-07-11 beacon-gating code currently lives in skcomms `cot_server.py`/`cot.py`; it
  moves with the extraction. Ensure the generic vs CoT-specific split (section 2) is exact so
  nothing generic is lost from skcomms and nothing CoT-specific is left behind.
- `cot_pki.py` (23k) carries TLS/data-package enrollment; it moves wholesale but verify it does
  not reach back into skcomms-private internals.
- GEO_STORE is currently referenced by `cot_service` only; confirm no core skcomms consumer.
