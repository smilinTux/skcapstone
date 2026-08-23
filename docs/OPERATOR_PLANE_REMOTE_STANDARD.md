# Operator-plane remote transport standard (skoperator.remote/v1)

Status: PROPOSED (design only; nothing in this doc changes a running service).
Companion: [OPERATOR_PLANE_MIGRATION.md](OPERATOR_PLANE_MIGRATION.md).
Evidence base: ATLAS Eyes first real run (PR #178), card 504d0046.

## 1. The problem, in one paragraph

The operator plane assumes every app's CLI is on the local PATH
(`Operatorapp.spec.cli`, run as `<cli> explain|observe|act`). Eyes proved that
contract is already dead on a single node: skgateway's declared cli is not on
PATH, skdashboard's exits 2 and has no in-process adapter (BLIND), fleet has no
registration at all, skbrain is invisible, and on 10 conditions the app's cli
lane and the seat's in-process lane return CONTRADICTORY values (card
504d0046). A distributed estate (skgateway moving to .100) makes "shell a local
binary" unsalvageable. This standard replaces the transport, not the
observation contract: `skoperator.observation/v1` envelopes stay the wire
payload.

## 2. Decisions at a glance

| # | Decision | Choice | Tradeoff accepted | Rejected alternative |
|---|----------|--------|-------------------|----------------------|
| 1 | Wire protocol | HTTP/1.1 + JSON, tailnet-bind only | No multiplexing, chattier than gRPC | gRPC/protobuf (toolchain + codegen weight for a 10-app estate); raw websocket (bidirectional channel invites observe/act blending) |
| 2 | Serving shape | ONE operator agent per node (extend `sknoded`), port 9392 | sknoded gains an HTTP surface (bigger blast radius on that daemon) | One daemon per app (N ports, N auth surfaces, N things to be down); riding skgateway (circular: cannot observe skgateway through skgateway, and it is FIRING today) |
| 3 | Self-served facets | An HTTP-native app MAY serve its own facet at a registered endpoint, same wire contract | Two implementation shapes to test | Forcing skgateway's facet through sknoded exec when the daemon already speaks HTTP |
| 4 | Streaming | SSE watch with cursor + poll fallback | One-directional only; reconnect logic on client | Websocket (adds keepalive/upgrade complexity and a write channel observation must never have); pure poll (not "more real time") |
| 5 | Consistency token | Per-node monotonic cursor (k8s resourceVersion analog), 410 Gone = relist | No cross-node global ordering | Global ordering (needs consensus we do not have and do not need) |
| 6 | Auth | capauth PGP request signing + capability scopes `operator.observe` / `operator.act` | Per-request signature verify cost (~ms) | New token scheme (violates house rule: auth is capauth); Tailscale-identity-only (node identity is not operator capability; no observe/act split) |
| 7 | Observe vs act | Separate URL trees, separate scopes; observe is GET-only and freeze-independent | Two grants to manage per client | One scope (would make observation require actuation rights, forbidden) |
| 8 | Lane authority | Exactly one authoritative producer per condition (the app's own facet on its home node); everything else advisory; two authoritative readings = hard `LaneConflict` | Seat built-in adapters demoted, eventually deleted | Silently preferring one lane (Eyes proved both lanes lie; preference launders lies) |
| 9 | Failure semantics | Unreachable / Unknown / Unauthorized are three named, distinct states; all map to `Unknown` status with distinct reasons, never healthy | More reason codes to handle | Collapsing to "down" or worse "no news is good news" |
| 10 | skos position | Read-only estate consumer via `/operator/v1/estate`, signed envelopes passed through with provenance | skos depends on control-plane node availability | skos re-probing apps itself (a second source of truth, forbidden) |
| 11 | `spec.cli` fate | Kept as node-LOCAL fallback executed only by the app's home-node agent; remote use forbidden; deprecated for new registrations | Legacy field lives on in the schema | Removing it (flag day) or keeping remote semantics (the exact bug we are fixing) |
| 12 | skgateway to .100 | Endpoint re-registration makes the move a one-field change, HARD-GATED on the .100 disk SEV2 closing | Move waits on disk work | Moving first (98% disk; an inference box at 5.3G free is an outage, not a home) |

## 3. Architecture: one wire contract, two serving shapes

```
ATLAS seat / eyes / skos (clients, capauth-signed requests)
        |            HTTPS-over-tailnet, GET only for observe
        v
+---------------------------+      +---------------------------+
| noroc2027 sknoded :9392   |      | .100 sknoded :9392        |
| operator agent            |      | operator agent            |
|  - execs local spec.cli   |      |  - execs local spec.cli   |
|  - hosts in-proc adapters |      |  - (after move) fronts    |
|  - /estate aggregate      |      |    nothing until apps land|
+---------------------------+      +---------------------------+
        ^
        | same wire contract, self-served
+---------------------------+
| skgateway :18780          |
| /operator/v1/... facet    |
+---------------------------+
```

- **Node operator agent**: `sknoded` (the existing per-node fleet daemon, our
  kubelet analog) grows an HTTP listener on the node's Tailscale address only
  (never 0.0.0.0), port **9392** (to be registered in PORTS.md and
  `FLEET_RESERVED_PORTS` at implementation). It serves observations for every
  Operatorapp homed on its node, by whichever local means the app provides:
  exec of the app's operator CLI (node-local, where PATH is actually true) or
  an in-process adapter.
- **Self-served facet**: an app that is already a long-lived HTTP daemon
  (skgateway today) MAY serve `/operator/v1/...` itself and register that
  endpoint. Same schemas, same auth, same failure taxonomy. This removes the
  dead-cli problem for skgateway without inventing a daemon.
- **What we take from Kubernetes and what we refuse.** Taken: the kubelet
  shape (one node agent fronting local workloads), watch + resourceVersion +
  410-relist, the healthz/readyz split, and "Unknown is a first-class status".
  Refused: etcd/consensus (Syncthing object store remains the spec plane),
  CRD/aggregation machinery, admission webhooks, client-go informer caches,
  leader election (noroc2027 is the always-on control plane by convention),
  and mTLS PKI (Tailscale provides the network boundary, capauth provides
  identity and capability).

## 4. API surface (`/operator/v1`)

| Verb + path | Scope | Semantics |
|---|---|---|
| `GET /operator/v1/healthz` | none (tailnet reachable) | Liveness of the agent process only. 200 = process up. Says NOTHING about apps. |
| `GET /operator/v1/readyz` | none | Readiness to serve AUTHORITATIVE observations. 503 + JSON list of failing dependencies (registry unreadable, capauth registry unavailable, clock skew). |
| `GET /operator/v1/apps` | `operator.observe` | Apps homed here: name, contractVersion, facet shape (exec / in-proc / self-served), endpoint. |
| `GET /operator/v1/apps/{app}/explain` | `operator.observe` | The app's explain payload, verbatim. |
| `GET /operator/v1/apps/{app}/observe` | `operator.observe` | One `skoperator.observation/v1` envelope, producer-signed (section 6). |
| `GET /operator/v1/observe?watch=1&cursor=N` | `operator.observe` | SSE stream of envelopes for all local apps (section 5). |
| `POST /operator/v1/apps/{app}/act` | `operator.act` (+ ratification) | OUT OF SCOPE for the observe migration. Reserved path; freeze checked server-side at the serving node on every call; never enabled by observe scope. |
| `GET /operator/v1/estate` | `operator.estate.read` | Control-plane node only: the eyes aggregate (all nodes, all apps, verdicts, conflicts) as one signed document. |

Health/readiness semantics, precisely:

- `healthz` false or unreachable: the NODE AGENT is the problem. Every app
  homed there becomes `Unknown (NodeUnreachable)`.
- `readyz` false: agent is up but its answers would not be authoritative;
  clients MUST treat observations from a not-ready agent as `Unknown
  (AgentNotReady)`, not as data.
- An app's own health is never inferred from either; it comes only from its
  observation envelope's conditions.

## 5. Watch semantics

- Each node agent keeps a per-node monotonic uint64 **cursor**, bumped on every
  new observation. SSE events carry `id: <cursor>`.
- Client reconnects with `cursor=<last seen>`. If the agent no longer holds
  that cursor (restart, ring buffer overrun): **410 Gone**, client does a full
  `GET .../observe` relist then re-watches. Exactly the k8s
  resourceVersion-too-old dance, minus etcd.
- Heartbeat comment frame every 30s so a dead TCP session is detected inside
  one minute. A watch with no heartbeat for 90s is dead: client MUST mark that
  node's apps `Unknown (WatchStale)` until relist succeeds, never keep serving
  the last frame as current.
- Poll fallback: plain GET on an interval is always legal. Watch is an
  optimization, never a correctness requirement.

## 6. Auth: capauth binding

- **Identity**: each client (ATLAS seat, eyes, skos, a human CLI) holds a PGP
  key registered in capauth. `~/.gnupg` remains the trust anchor, outside the
  Syncthing-replicated store, exactly as documented.
- **Request signing**: every request carries headers
  `X-SK-Fingerprint`, `X-SK-Timestamp`, `X-SK-Nonce`, `X-SK-Signature`
  (detached PGP signature over `method\npath\nsha256(body)\ntimestamp\nnonce`).
  Server verifies against the capauth registry, rejects skew > 120s and
  replayed nonces (per-node LRU).
- **Capabilities** (capauth grants, verified per request):
  - `operator.observe` (optionally scoped `app:<name>` / `node:<name>`)
  - `operator.act` (always app-scoped; additionally gated by human
    ratification and freeze, unchanged from today)
  - `operator.estate.read` (the aggregate)
  - Observe NEVER implies act. Act does not imply estate.read. Freeze status
    is checked at the serving node at act time; observation ignores freeze by
    construction (GET-only, no state transition anywhere in the observe tree).
- **Envelope signing**: the producer (node agent or self-served facet) signs
  the canonical JSON of each observation envelope and attaches
  `signature` + `signer_fpr`. Consumers re-serving an envelope (estate, skos)
  MUST pass signature and provenance through unmodified. This is what makes a
  second source of truth structurally impossible: an unsigned or re-signed
  "observation" is not an observation.

## 7. Failure taxonomy (fail closed, three distinct states)

All failures surface as condition status `Unknown` with a DISTINCT reason.
None of these may ever render as healthy, and the three families must never
collapse into each other:

| Family | Reason codes | Meaning |
|---|---|---|
| **Unreachable** | `NoEndpoint`, `NodeUnreachable`, `ConnectTimeout`, `WatchStale`, `AgentNotReady` | Could not get an answer. Says nothing about the app. |
| **Unknown** | `ProbeFailed`, `ProbeTimeout`, `Unparseable`, `Absent` (declared condition missing from payload), `Expired` (ttl passed), `LaneConflict` | Got an answer that cannot be trusted or is missing. |
| **Unauthorized** | `Unauthorized` (401/403), `SignatureInvalid`, `CapabilityMissing` | Was refused. A security signal, not a health signal; MUST also raise its own alert because a misconfigured grant can blind the whole seat. |

Timeouts (constants, one place): connect 2s, request 10s (matches today's
`SUBPROCESS_TIMEOUT`), watch heartbeat 30s, watch declared dead 90s, envelope
default ttl 300s.

## 8. Lane authority (the 504d0046 rule)

- Every condition has EXACTLY ONE authoritative producer: **the app's own
  operator facet, executed on the app's home node** (via its node agent, or
  self-served). That is where PATH, sockets, and files are real.
- The seat's in-process adapters become ADVISORY the moment an app's endpoint
  registration goes live, and are deleted after the parity gate
  (migration doc, phase 5). Advisory readings carry
  `provenance: seat-builtin:<app>` and may be used only for conflict
  detection, never for verdicts.
- A consumer that holds two AUTHORITATIVE readings for one condition (a
  migration bug, a double registration) MUST emit `Unknown (LaneConflict)` and
  raise a hard error. Silently preferring either lane is forbidden: Eyes
  proved disagreement happens in BOTH directions on the same app.
- During migration, conflict between authoritative and advisory lanes is a
  FIRING signal on the migration itself (it means a lane is lying) and blocks
  that app's cutover from completing.

## 9. Versioning

- URL path version `/operator/v1/`; breaking wire changes mean `/v2` served
  alongside, never in place.
- Envelope schema string (`skoperator.observation/v1`) is authoritative for
  payload shape; consumers reject schemas they do not know (Unknown, reason
  `Unparseable`), never best-effort parse.
- `Operatorapp.spec.contractVersion: 2` introduces `endpoint` (URL),
  `node` (home node name), and `transport: "http"|"cli-local"`. Version 1
  specs remain valid and mean "cli-local on the registering node" (the
  fallback, section 11 of the migration doc).

## 10. Freeze independence (invariant, restated as a test obligation)

ATLAS is frozen and stays frozen. The observe tree is GET-only, touches no
fleet object, no timer, no ITIL record, and requires only `operator.observe`.
The implementation MUST carry Eyes' byte-identical-tree test forward to the
HTTP path: a full estate observation over the network leaves every fleet store
byte-identical. Act rides a separate scope and separate path, and the freeze
check happens server-side at the app's home node, so a remote seat cannot act
around a freeze even with a stolen observe grant.

## 11. skos: the meta-level consumer

skos is the OS-level perspective. It gets what ATLAS has, not a copy of
ATLAS's job:

- skos reads `GET /operator/v1/estate` on the control-plane node agent
  (noroc2027) with an `operator.estate.read` grant. One hop, one signed
  document, provenance intact.
- skos MAY cache the estate document but MUST retain `observed_at` and ttl and
  render expiry as `Unknown (Expired)`. It MUST NOT probe apps itself, MUST
  NOT re-emit observations, and anything it derives (rollups, GTD captures,
  dashboards) is a different schema (`skoperator.rollup/v1`) referencing
  source envelopes by app + cursor, so a rollup can never be mistaken for an
  observation.
- Tradeoff accepted: if noroc2027 is down, skos is blind. That is correct
  behavior (it renders the whole estate `Unknown (NodeUnreachable)`), and
  noroc2027 is the always-on node by fleet convention. The alternative (skos
  fanning out to every node) doubles the client matrix and is exactly how a
  second source of truth starts.

## 12. Non-goals

- No act migration in this standard (path reserved, semantics unchanged).
- No new auth scheme, no new message bus, no per-app daemons.
- No change to `skoperator.observation/v1` payload semantics: polarity stays
  schema-owned, Unknown stays first-class, TTL stays mandatory.
