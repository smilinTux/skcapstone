# SKWorld Fleet Control Plane: Design Spec

Date: 2026-07-27 (rev 2, same day: operator-seat assessment folded in)
Author: Fable (architect pass; rev 2 adds the cognitive layer, events, signing,
self-bootstrap, right-sized complexity, and the scheduler v1 descope)
Status: PROPOSED (epic spec + phased roadmap)
Scope: whole fleet (.158, .41, .100, local box)

## 1. Summary

SKWorld adopts the Kubernetes / WebSphere control-plane PATTERN at fleet scope:
a central store of declarative desired state, a scheduler that places work, and
per-node agents that reconcile local reality to the declared plan. We adopt the
pattern, not the machinery. No etcd, no Raft, no overlay network, no service
mesh, no ingress stack, no XML config-push ceremony. For a 2-4 node fleet the
Syncthing-shared coord board already IS the datastore and the API surface. The
work of this epic is to unify assets we already run (autopilot, skscheduler,
the skcapstone daemon, trustee tools, doctor, autoscale, skvault) under one
declarative object model and one reconcile discipline.

The plane has two layers, named explicitly because they have different actors
and different guardrails:

- The AUTONOMIC layer: mechanical controllers (scheduler, sknoded, the kind
  controllers) that reconcile observed reality to declared spec. Dumb,
  level-triggered, idempotent. This is most of the epic (Phases 1 to 7).
- The COGNITIVE layer: an AI operator that reads conditions, events, and
  alerts, and sets spec from observation, within hard guardrails (dry-run,
  freeze kill-switch, approval gate, signed writes; section 8). The autonomic
  layer gives the fleet a spine; the cognitive layer is the north-star payoff
  of the whole epic: scaffolding that gives an AI arms and legs to manage the
  entire fleet through the same surface a human uses.

A second stated principle: RIGHT-SIZED COMPLEXITY (section 3.6). The same
objects and the same code run a one-box minimal fleet with near-zero overhead
and scale to N nodes by declaring more objects. Complexity is proportional to
objects declared, never to machinery installed. The plane also bootstraps
itself from a single fresh box with no manual file authoring (section 9). In
Chef's words: as complex or as simple as it needs to be.

Explicit non-goals (machinery we will NOT build):

- No consensus layer (etcd/Raft). Syncthing last-writer-wins plus per-file
  single-writer ownership is the consistency model.
- No API server process. Files are the API. MCP/CLI tools are the client.
- No network overlay, mesh, or ingress. Tailscale plus existing ports stand.
- No admission webhooks, RBAC, or multi-tenancy. capauth already gates access.
- No live config push. Syncthing distributes config; controllers notice.

## 2. The pattern, mapped

| Ours today | Kubernetes | WebSphere | Becomes |
|---|---|---|---|
| Coord board (Syncthing JSON cards) | etcd + API objects | dmgr master repo | Fleet object store (extended) |
| `skos autopilot run` | `kubectl apply` | wsadmin | JobController reconcile |
| Per-host ThreadPool + Docker sandbox | kubelet + pods | node agent + app servers | sknoded (node agent) |
| autoscale.py (min/recommended/max) | HPA / cluster-autoscaler | manual tuning | Capacity probe library |
| `--tag` / `--tasks` selection | label selectors | (none) | Selectors on all kinds |
| cleanup.py (cold/teardown/off) | pod GC | (none) | GC policy per kind |
| skscheduler | CronJob controller | (none) | CronController |
| trustee_* MCP tools | kubectl rollout/logs/scale | admin console ops | Node-agent actuation library |
| skcapstone doctor / selftest | node conditions, probes | PMI health | Condition probes |
| skvault + Syncthing | Secrets + ConfigMaps | (none clean) | ConfigController (wrap) |

## 3. Substrate: the coord board as API server

### 3.1 Where objects live

Fleet objects are JSON files under the already-synced `~/.skcapstone/` share:

```
~/.skcapstone/fleet/
  objects/                      # DESIRED state (spec). One file per object.
    node/<name>.json            # node-158.json, node-41.json, node-100.json, node-local.json
    service/<name>.json
    cronjob/<name>.json
    agent/<name>.json
    modelserver/<name>.json
    config/<name>.json
  placements/                   # Scheduler output. One file per placed object.
    service/<name>.json
    job/<card-id>.json          # only for jobs the scheduler places (autopilot dispatch)
  status/                       # OBSERVED state. Partitioned by writer node.
    <node>/
      heartbeat.json            # single file, overwritten in place
      node.json                 # capacity + conditions, overwritten in place
      service/<name>.json       # per-unit observed status
      modelserver/<name>.json
      agent/<agent>.json
```

Jobs (BuildCards) do NOT move: the existing event-sourced coord cards at
`~/.skcapstone/coordination/tasks/` remain the Job store, accessed through the
existing `coord_*` MCP tools. The fleet layer only adds a placement record for
jobs that get dispatched cross-node.

### 3.2 The single-writer ownership rule (the load-bearing rule)

Syncthing is eventually consistent and resolves conflicts per FILE with
last-writer-wins. Therefore per-field ownership is implemented as per-FILE
ownership: every file in the fleet tree has exactly one writer in the whole
fleet, ever. No file is ever modified by two processes on two nodes. Spec and
status are separate files, not sections of one file. The "object" a reader sees
is assembled at read time by merging spec + placement + status.

Concrete ownership table:

| File | Sole writer | Runs on |
|---|---|---|
| `objects/node/<name>.json` (labels, taints, cordon) | Operator via `skfleet` CLI, applied by NodeController | control-plane node (.158) |
| `objects/service/*.json`, `objects/cronjob/*.json`, `objects/agent/*.json`, `objects/modelserver/*.json`, `objects/config/*.json` | Operator via `skfleet apply` (controllers never edit spec of their own kind's objects; they only read spec and write their kind's status or placements) | control-plane node (.158) |
| `placements/**` | Scheduler | control-plane node (.158) |
| `status/<node>/**` | sknoded on that node, and only that node | each node writes only its own subtree |
| Coord cards (Jobs) | Existing event-sourced coord store (append-only events, already conflict-safe) | unchanged |

Corollaries:

- A node NEVER writes another node's status subtree. This is a hard invariant,
  enforced by sknoded refusing to write outside `status/$(self)/`.
- The scheduler NEVER writes status. sknoded NEVER writes placements or spec.
- Controllers other than the scheduler run on the control-plane node and write
  only within their designated output (mostly: nothing; they act by writing
  placements via the scheduler path or by calling actuation on their own node).
- Retirement of an object = the spec writer sets `spec.deleted: true`
  (tombstone field inside the file, in place). File deletion happens only in a
  periodic GC pass on the control-plane node after all status references age
  out. This avoids Syncthing tombstone churn (lesson from the 1.5M-tombstone
  outbox flood).
- Every spec file carries a `generation` (int, bumped on every spec write).
  Every status file carries `observedGeneration`. Staleness under eventual
  consistency is therefore always detectable, never silent.
- Heartbeats are ONE small file per node, overwritten in place at a fixed
  low rate (default 60s). No fan-out, no per-beat files, no broadcast. This is
  a direct response to the broadcast-heartbeat flood that crippled .41.

### 3.3 Consistency and timing budget

Syncthing propagation on this fleet is seconds under normal load. All control
loops are level-triggered and idempotent, so a decision made on stale state is
corrected on the next pass, never compounded. Timing constants are chosen so
that sync latency is negligible relative to decision windows:

- heartbeat write interval: 60s
- node NotReady threshold: 3 missed beats (180s), node Dead: 300s
- scheduler reconcile interval: 60s (plus on-demand on `skfleet apply`)
- sknoded reconcile interval: 30s
- service restart backoff: 10s doubling to 5m cap (crash-loop guard)

Rule of thumb encoded in code: no controller may take an irreversible action
(e.g. rescheduling a service off a node) on data younger than 2x the sync
budget or on a single missed observation.

### 3.4 Conflict handling

Conflicts should be impossible by construction (single writer per file). If a
`.sync-conflict` file ever appears under `fleet/`, that is a BUG in ownership
discipline: doctor gains a probe that detects conflict files, raises a fleet
Condition, and alerts via sk-alert. We do not auto-merge conflicts.

### 3.5 Events, not just conditions

Conditions answer "what is true now". Events answer "why did X happen and
when". The cognitive layer (section 8) cannot reason about causality from a
snapshot, so every kind emits events from day one: the store primitive lands
in Phase 1 (Card 1.1), before any controller exists to use it.

Design, flood-safe by construction (same discipline as heartbeats, R2):

- One append-only JSONL event file per NODE, never per object and never per
  event: `status/<node>/events.jsonl`. The single-writer-per-node invariant
  holds for Syncthing purposes; multiple fleet processes on the same node
  (sknoded plus control-plane components on .158) append through the store
  library under a local flock with O_APPEND.
- An event line: `{ts, node, kind, name, type, reason, message, count}`.
  Readers filter by (kind, name) to reconstruct one object's causal history.
- Bounded: size-capped rotation (default 1 MB), keeping exactly one rotated
  file (`events.jsonl.1`, overwritten on the next rotation). Two files per
  node, ever.
- Rate-capped and write-on-change: an event identical to the last one for the
  same (kind, name, type, reason) within the dedupe window (default 5 min)
  bumps an in-memory count instead of appending. Event churn counts against
  the R2 churn cap and is measured in the Phase 1 baseline.
- Events are observability, not control flow: no controller may key a
  decision off the event log (decisions read spec + placement + status +
  conditions only). Loss under rotation is therefore always harmless.

### 3.6 Right-sized complexity (a tested invariant)

Chef's framing is binding: the plane must be "as complex or as simple as it
needs to be". We make that a first-class, TESTED invariant, not a vibe:

- The same objects and the same code run a 1-box minimal fleet: no scheduler
  needed (placement is trivial when one node exists), no controllers beyond
  sknoded keeping local services up, and `skfleet` works against the local
  tree alone.
- A kind with zero objects is a no-op that costs nothing: no directories
  demanded, no reconcile work, no status files, no events. Complexity equals
  objects declared.
- Scale-up is additive only: adding node two means enrolling node two
  (section 9), never reconfiguring node one.

Card 1.1 carries a single-node-mode test proving this invariant, and every
later phase must keep it green. A phase that breaks one-box operation is
rejected in review, the same as an R5 scope-creep violation.

## 4. Object conventions

Every spec file:

```json
{
  "kind": "Service",
  "name": "skgateway",
  "labels": {"tier": "core"},
  "generation": 7,
  "spec": { ... }
}
```

Every status file:

```json
{
  "kind": "Service",
  "name": "skgateway",
  "node": "node-158",
  "observedGeneration": 7,
  "status": { ... },
  "conditions": [
    {"type": "Ready", "status": "True", "reason": "UnitActive",
     "message": "systemd unit active 4d", "lastTransition": "2026-07-27T09:00:00Z"}
  ],
  "updatedAt": "2026-07-27T09:12:00Z"
}
```

Conditions convention (used by every kind): a list of
`{type, status: True|False|Unknown, reason, message, lastTransition}`.
Standard Node condition types: `Ready`, `MemoryPressure`, `DiskPressure`,
`GPUAvailable`, `Reachable`. Standard Service condition types: `Ready`,
`Progressing`, `CrashLooping`. `Unknown` is mandatory when data is stale
(observedGeneration behind, or heartbeat past threshold): controllers must
treat Unknown as "do not act aggressively", not as False.

Selectors: `spec.nodeSelector` is a label match map (exact match, AND
semantics, same as autopilot `--tag` semantics today). Taints on Nodes carry
`effect: NoSchedule | PreferNoSchedule`; workloads list `tolerations`.

## 5. Resource model

### 5.1 Node

- Spec (operator-owned): `labels` (heavy-build, gpu, travel, always-on),
  `taints`, `cordoned: bool`, `capacityOverrides` (optional manual caps),
  `address` (LAN + tailscale addresses, ssh target).
- Status (sknoded-owned): `capacity` {cores, ramGB, diskGB, gpu model/VRAM}
  and `allocatable` (capacity minus reserves), both computed by the existing
  `skharness.autocode.autoscale` probing reused as a library; `load` snapshot;
  `heartbeat` timestamp; `conditions`; `versions` (sknoded, skcapstone, python).
- Owning controller: NodeController (on .158) computes derived health
  (Ready/NotReady/Dead) from heartbeat age plus conditions, and is the only
  component allowed to mark a node schedulable or not in the scheduler's view.
  sknoded self-reports raw observations only.
- Reconcile trigger: heartbeat file mtime change, or 60s tick.

Node objects are not hand-authored: a node self-reports a join request and an
admission step mints the spec (section 9). The operator (or an auto-approve
policy) supplies labels and taints at admit time.

Initial fleet objects (minted via `skfleet admit`, values below):

- `node-158`: labels {always-on, dev-primary, control-plane}; no taints.
- `node-41`: labels {heavy-build}; taint `travel=true:PreferNoSchedule` set
  by the operator when the box travels (today it is tailscale-only at
  100.86.156.5); address records both LAN and tailscale.
- `node-100`: labels {gpu}; taint `dedicated=model-serving:NoSchedule` so only
  tolerating workloads (model servers, embeddings, ComfyUI) land there.
- `node-local`: labels {interactive}; taint
  `interactive=true:PreferNoSchedule` (16GB, 4 cores, keep it responsive).

### 5.2 Service (long-running workloads)

Targets: skchat daemon, skgateway, skcomms, skmemory daemon, ollama,
skwhisper@agent, coturn, model servers (as ModelServer, see 5.6), piper-tts,
nostr relay.

- Spec: `runtime` (systemd-user | docker), `unit` (unit name or compose ref),
  `replicas` (almost always 1), `nodeSelector`, `tolerations`,
  `resources` (requested cores/RAM, advisory), `healthCheck` (doctor probe
  name or port check), `restartPolicy`.
- Status (per node running it): `state` (active/failed/missing), `pid`,
  `restarts`, `since`, `conditions`.
- Placement (scheduler-owned): `placements/service/<name>.json` listing target
  node(s) with `placementGeneration`.
- Owning controller: ServiceController (control-plane side) decides desired
  placement changes and requests them via the scheduler; sknoded (node side)
  actuates: reads placements addressed to itself, drives `systemctl --user`
  and Docker to match, using the trustee_* operation verbs as its actuation
  library.
- Reconcile trigger: spec generation change, placement change, unit state
  change (sknoded watches systemd), 30s tick.
- This kind is the highest visible payoff: declarative, self-healing
  fleet-wide deploys replacing hand-run systemctl on each box.

Failover semantics (deliberately conservative for v1): if a Service's node
goes Dead, ServiceController re-places only if `spec.failover: auto` is set.
Default is `manual` with an sk-alert, because most of our services are
node-bound by data locality (skmem-pg is local-per-node by prior incident
decision, and stays OUT of fleet management except as a health condition).

### 5.3 Job / BuildCard (run to completion)

- Spec: the existing coord card, unchanged. Cards are event-sourced and
  conflict-safe already. Label-selector scoping is the existing
  `--tasks`/`--tag` mechanism.
- Status: the existing card lifecycle (`coord_claim`, `coord_complete`,
  `coord_move`) plus the autopilot run ledger. No new status files.
- Placement: for cross-node dispatch the scheduler writes
  `placements/job/<card-id>.json`; the autopilot executor on the target node
  claims the card through the normal `coord_claim` path (claim remains the
  authoritative "I am running this" signal, so a stale placement can never
  cause double execution: claim is atomic in the event-sourced store).
- Owning controller: JobController = the existing autopilot harness
  (`skharness.autocode.orchestrator` + `executor`), re-homed conceptually, not
  rewritten. The only code change: `dispatch` consults Node objects (alive,
  headroom, taints) instead of the static dual-node (.158 + .41) list, and
  `autoscale.py` numbers come from the Node status it already computes.
- Reconcile trigger: new/uncompleted cards matching selection scope, on
  `skos autopilot run` or CronJob tick.
- GC: existing cleanup.py (cold/teardown/off) is the Job GC policy, unchanged.

### 5.4 CronJob

- Spec: `schedule` (cron expr), `jobTemplate` (what to invoke: autopilot run,
  backup script, digest, 7:15 brief), `node` or `nodeSelector`,
  `concurrencyPolicy` (forbid/replace), `missedRunPolicy`.
- Status: `lastRun`, `lastResult`, `nextDue`, `conditions` (e.g. `MissedRun`).
- Owning controller: CronController = existing skscheduler
  (`skcapstone scheduler`). It gains a config adapter that reads CronJob
  objects from the fleet tree in addition to its current job definitions, and
  writes run status into `status/<node>/cronjob/<name>.json` on the node where
  it ran. Existing jobs (autopilot-daily, backups, digests, brief) are
  migrated to CronJob objects one by one; skscheduler internals stay.
- Reconcile trigger: time tick; spec generation change reloads the schedule.

### 5.5 Agent (our special sauce, no K8s analog)

- Spec: `soul` (active blueprint name, e.g. lumina-unhinged), `model`
  (selection from the deployed 128-model catalog, per-agent), `daemon`
  {enabled, nodeSelector} (where the consciousness loop runs), `channels`
  (telegram/bridges on/off), `skwhisper: bool`.
- Status: `daemonNode`, `daemonState`, `activeSoul` (as loaded),
  `activeModel` (as routed), `lastHeartbeat` (consciousness loop),
  `conditions` (`SoulLoaded`, `ModelRoutable`, `DaemonReady`).
- Owning controller: AgentController. It WRAPS what exists: the skcapstone
  per-agent daemon, the soul `active.json` toggle (SystemPromptBuilder already
  hot-reloads on a 60s cache), and the deployed model-switching plumbing
  (catalog, per-agent selection across daemon/bridges/telegram/app). The
  controller's job is drift detection and convergence: if spec says model X
  and the routing says Y, converge and record a condition; if the daemon
  should run on a node matching the selector and does not, start it via the
  Service machinery (an Agent's daemon is internally realized as a Service
  object it owns, prefixed `agent-<name>-daemon`).
- Reconcile trigger: spec change, daemon heartbeat change, 60s tick.

### 5.6 Model / ModelServer

- Spec (ModelServer): `engine` (llama.cpp | ollama | vllm-later), `models`
  (list of catalog ids), `nodeSelector` (gpu), `port`, `ctx`, `vramBudget`.
  Spec (Model catalog): REUSED as-is from the deployed model-registry, not
  duplicated; fleet objects reference catalog ids only.
- Status: `loadedModels`, `port health`, `vramUsed`, `conditions`
  (`Serving`, `CatalogSynced`).
- Owning controller: ModelController. Wraps the existing catalog +
  skgateway routing (`sk-default` auto-route stays authoritative for
  inference routing). The controller reconciles which model servers run where
  (today: ornith/qwen3.6 on .100:8082, ollama :11434, embeddings :11435) and
  keeps skgateway's upstream list in sync with observed Serving status, so a
  dead model server is visible as a condition instead of silent 502s.
  Explicitly: skgateway's `sk-default` auto-router already falls back around
  dead upstreams; ModelController only FEEDS upstream health into skgateway
  and never reimplements routing or fallback. No second router.
- Reconcile trigger: spec change, health probe transition, 120s tick.

### 5.7 Secret / Config

- Spec (Config): `entries` (list of {skvaultEntry, targetPath or env, nodes}),
  `rotationMaxAgeDays` (optional). Secret MATERIAL never enters fleet
  objects: only skvault entry NAMES are referenced. skvault (KeePass sealed
  to Chef's PGP) remains the sole secret store; Syncthing remains the config
  transport.
- Status: `present` per node, `age` per entry, `conditions`
  (`SecretPresent`, `RotationOverdue`, `ConfigDrift`).
- Owning controller: ConfigController, a thin WRAP: it verifies presence and
  age, hashes deployed config files against expected, and raises conditions.
  It does not push secrets; deployment of secret material stays a manual or
  scripted skvault operation. This gives us the audit surface (the
  "RotationOverdue" condition would have caught the gog keyring and NVIDIA
  key incidents earlier) without building a secret-distribution machine.
- Reconcile trigger: 6h tick plus on-demand `skfleet config verify`.

## 6. The node agent: sknoded

A single thin per-node daemon (systemd --user unit `sknoded.service`), the
kubelet analog. Lives in the skcapstone repo (`skcapstone/fleet/agent.py`),
because heartbeat, doctor, trustee verbs, and the daemon runtime already live
there. It is a loop, not a framework:

1. Self-report: every 60s write `status/$(self)/heartbeat.json` and
   `status/$(self)/node.json` (capacity via the autoscale probe library,
   conditions via doctor probes). In-place overwrite, single file each,
   rate-capped. Never writes outside its own subtree.
2. Actuate: every 30s read `placements/` entries addressed to itself and the
   corresponding Service specs; diff against local systemd --user and Docker
   state; converge using the trustee operation verbs (restart, scale, logs
   capture on failure). Crash-loop backoff with a `CrashLooping` condition
   instead of infinite restarts.
3. Probe: run the healthCheck for each locally placed Service, write per-unit
   status files.
4. Degrade safely: if the fleet tree is unreachable or stale (Syncthing down),
   sknoded keeps last-known placements running (static-pod behavior) and marks
   its own status `Unknown` on recovery. It never stops services just because
   it cannot read spec.

sknoded on the local box runs in report-only mode initially (self-report but
no actuation) until we opt it in.

## 7. The scheduler

One scheduler process, on the control-plane node (.158), run under skscheduler
as a CronJob-like tick plus on-demand invocation from `skfleet apply`. Static
single leader by convention: no lease, no election. Rationale: a
Syncthing-mediated lease cannot prevent split-brain, so we do not pretend to
have one; instead the scheduler is stateless and idempotent, and starting it
on .41 during a .158 outage is a one-command manual failover documented in a
runbook. Because every reconcile pass recomputes from spec + status, a
takeover cannot corrupt anything, and double-run of jobs is prevented at the
coord-claim layer, not the scheduler layer.

Placement algorithm (deliberately small):

1. Filter: nodes must be Ready (per NodeController), not cordoned, match
   `nodeSelector`, tolerate all `NoSchedule` taints, and have allocatable
   headroom for the workload's requested resources (headroom from Node status,
   which is autoscale.py output).
2. Select (v1, deliberately minimal): pick the least-loaded survivor (most
   allocatable headroom), with a single deterministic tiebreak (lexicographic
   node name). Nothing else.
3. Write `placements/<kind>/<name>.json` with the chosen node and
   `placementGeneration`. Never write status, never touch spec.

Deferred to a v1.1 card, gated on a demonstrated placement-contention need
(a carded incident or a measured bad placement, per right-sized complexity):
preference scoring (`PreferNoSchedule` ordering, so .41 traveling is avoided
but usable and the local box is a last resort) and label-affinity bonuses.
In v1 a `PreferNoSchedule` taint is advisory only: recorded in the placement
reason, not acted on. `NoSchedule` filtering and headroom filtering stay in
v1, because those are correctness, not preference. The scheduler also checks
the freeze flag (section 8) and writes no placements while frozen.

The autopilot dispatcher becomes the first scheduler client: its dual-node
selection (.158 + .41) is replaced by a scheduler query, which is how phase 2
ships value without touching the build engine itself.

## 8. Operating the plane: the operator seat

Today every file under `objects/` is human-owned via `skfleet apply`. That
stays true through the early phases, but the design names the end state now:
the spec writer is a SEAT, and the seat can be held by Chef, by a script, or
by an AI operator. The two layers from section 1, restated as actors:

- AUTONOMIC (mechanical): controllers reconcile reality to spec. They never
  originate intent; they converge to it.
- COGNITIVE (AI operator): a loop that reads merged objects, conditions, the
  event log (3.5), and alerts, then proposes and (within guardrails) applies
  spec diffs. It originates intent the way a human operator does, through
  exactly the same file surface and CLI. No private side-channel, no second
  API. This is the north-star payoff and lands as Phase 8.

Guardrails, all mandatory before the cognitive seat goes live:

1. Dry-run: `skfleet apply --dry-run` renders the predicted effect (spec
   fields changed, predicted placement changes, actuation the fleet would
   take) without writing anything. Propose-with-dry-run is the AI operator's
   default posture.
2. Freeze / kill-switch: a single flag file, `objects/_freeze.json`, owned by
   the human operator. While it reads `{"frozen": true}`, sknoded halts ALL
   actuation (running services are left running; self-report continues) and
   the scheduler writes no placements. One file, checked before every
   actuation, no ambiguity. The store primitive (`is_frozen`) lands in
   Phase 1 so every later component is born checking it.
3. Approval gate: high-blast-radius actions (fleet-wide restart, drain of a
   node running an always-on service, object delete) never actuate directly.
   They park as a pending-approval record that a human resolves via
   `skfleet approve` or `skfleet reject` (or that expires). The gate binds
   the ACTION class, not the actor, so a human fat-finger is caught by the
   same net.
4. Signed writes (R6 in section 11): every spec and placement write carries
   the writer's capauth/PGP identity and signature; sknoded verifies before
   actuating. The AI operator signs as its own agent identity, so the audit
   trail distinguishes seats.

Self-describing surface: `skfleet explain [kind]` lists kinds, their
spec/status fields, condition types with meanings, and available actions,
with `--json` machine-readable output. A fresh AI operator discovers the
system at runtime instead of via hardcoded knowledge. The seam ships with
the CLI in Phase 1 (Card 1.3); the capstone fills it out in Phase 8.

## 9. Bootstrap: self-enrollment, admission, cold start

The plane bootstraps itself, literally: a fresh box joins by running sknoded,
not by a human authoring JSON files.

Join flow:

1. A fresh box installs skcapstone and starts sknoded, with the shared
   Syncthing tree reachable (or created locally on the very first machine).
2. sknoded finds no `objects/node/<self>.json`. It writes ONLY its own
   `status/<self>/` subtree: heartbeat, node.json, and a join marker
   `status/<self>/join.json` with {name, addresses, capacity snapshot,
   capauth public key fingerprint, requestedAt}. Ownership rules hold even
   here: a joining node never touches `objects/`.
3. Admission mints the spec: the operator runs `skfleet admit <node>
   [--labels ... --taints ...]`, which reads the join request and writes
   `objects/node/<name>.json`. An optional auto-approve policy admits nodes
   whose capauth key is already trusted (known-key nodes), for hands-free
   rebuild of an existing box.
4. Until admitted, the node schedules nothing and actuates nothing; it only
   self-reports. `skfleet nodes` shows it as Pending.

Control-plane cold start (single box up to full plane), documented as a
runbook and honored by the code's degrade rules:

1. skcapstone daemon and Syncthing up (the tree exists locally even with no
   peers yet).
2. sknoded starts, self-reports, writes its join request.
3. First-node special case: `skfleet admit --bootstrap <self>` mints the
   first node object locally (there is no other operator seat yet).
4. NodeController starts (a tick under skscheduler on the control-plane
   node) and derives Ready.
5. Scheduler starts; with one node, every placement is trivial (3.6).
6. Controllers become self-hosted: sknoded, the scheduler tick, and the
   controller ticks are themselves declared as Service/CronJob objects, so
   from here on the plane keeps its own components converged like any other
   workload.

There is no manual file authoring anywhere on the path from bare box to
managed fleet. That is the literal reading of "a system that can bootstrap
itself", and it composes with 3.6: one admitted node is already a complete,
working plane.

## 10. Unification map: reuse, wrap, replace

Bias is hard toward reuse. Nothing in this epic greenfields a controller that
exists in scattered form.

| Asset | Module / repo | Verdict | How it fits |
|---|---|---|---|
| Autopilot harness (build, grade, twin gate, automerge) | `skharness.autocode` (`orchestrator.py`, `executor.py`), re-exported by `skos.autopilot` | REUSE as-is | It IS the JobController. Only its dispatch node-selection changes (consult Node objects). |
| autoscale.py | `skharness.autocode.autoscale` | REUSE as library | Becomes the capacity probe inside sknoded self-report. Single source of capacity math fleet-wide. |
| cleanup.py (cold/teardown/off) | `skharness.autocode.cleanup` | REUSE as-is | Job GC policy, unchanged. |
| `--tasks` / `--tag` selection | autopilot CLI | REUSE | The selector semantics for all kinds copy this behavior. |
| skscheduler | `skcapstone scheduler` (scheduler_* modules) | WRAP | Becomes CronController via a config adapter reading CronJob objects; internals and existing jobs untouched during migration. |
| skcapstone daemon (consciousness loop, heartbeat, comms) | skcapstone | REUSE | The workload AgentController manages; daemon itself unchanged. |
| heartbeat MCP tools (`heartbeat_pulse/peers/health/find_capable`) | skcapstone | WRAP | sknoded self-report replaces ad-hoc pulses for fleet purposes; the MCP tools become readers of Node status so agents keep their view. |
| trustee_* tools (deployments/health/restart/scale/rotate/logs/monitor) | skcapstone | WRAP | The actuation verb library inside sknoded; also stay available as manual MCP tools. |
| doctor / selftest | skcapstone (`doctor*`) | WRAP | Probes re-exposed as Condition producers; doctor CLI output gains fleet conditions. |
| Coord board + `coord_*` MCP tools | skcapstone coordination | REUSE as-is | The Job store and the claim/complete lifecycle. Fleet tree extends the same Syncthing share, same tooling philosophy. |
| skvault | skvault repo | REUSE as-is | Sole secret store. ConfigController references entries by name only. |
| Syncthing | infra | REUSE as-is | The replication transport for spec/placement/status files. |
| Model-switching (catalog, per-agent selection, routing) | deployed across daemon/bridges/telegram/app + skgateway | REUSE | ModelController wraps it for placement + health; catalog stays the model source of truth. |
| skgateway routing (`sk-default`) | skgateway | REUSE | Inference routing authority; ModelController only feeds it upstream health. |
| WebSphere-style config push | n/a | REPLACE (never build) | Explicitly rejected machinery. |
| Static dual-node dispatch list in autopilot | autopilot config | REPLACE | The one true replacement in this epic: superseded by scheduler placement. |

New code, kept small: `skcapstone/fleet/` (object model + file store helpers
+ event log + freeze primitive, NodeController, ServiceController,
ConfigController glue, sknoded loop, admission, `skfleet` CLI: `apply`
(with `--dry-run`), `get`, `describe`, `explain`, `nodes`, `cordon`, `drain`,
`admit`, `freeze`/`unfreeze`, `approve`/`reject`). Scheduler is a module in
the same package. Everything else is adapters.

## 11. Risks

### R1 (governing): Syncthing eventual consistency

The flood incident (broadcast heartbeats, 1.5M tombstones, .41 crippled) and
the skmem-pg replication drift (forced local-per-node primaries) are the two
scars that shape this design. Mitigations, all structural:

- Single writer per FILE, fleet-wide, as the hard invariant (section 3.2).
  Ownership is per-file precisely because Syncthing conflicts are per-file.
- Bounded file count: heartbeat and node status are one overwritten file per
  node, statuses are one file per (node, unit). No per-event files, no
  fan-out, no broadcast. GC by in-file tombstone flag first, physical delete
  later, to avoid tombstone churn.
- `generation` / `observedGeneration` on everything: staleness is detectable,
  and controllers must treat stale data as `Unknown`, not `False`.
- Level-triggered idempotent reconcile: a decision on stale state is corrected
  next pass; nothing edge-triggered, nothing compounding.
- Timing budget: decision windows (180-300s) dwarf sync latency (seconds), and
  no irreversible action on a single missed observation.
- Double-execution safety does not rest on the scheduler: Jobs are guarded by
  atomic coord claims, Services by systemd unit idempotence on a named node.
- Doctor probe for `.sync-conflict` files under `fleet/` with sk-alert: a
  conflict file means an ownership bug, treated as an incident, never merged.

### R2: Reintroducing a sync flood

The fleet tree adds write traffic to the same Syncthing share that melted
before. Mitigations: fixed write rates (60s heartbeat, 30s actuation status
only on change), write-on-change-else-skip for all status files (no-op writes
suppressed, same discipline as gtd upsert `unchanged`), a hard cap alarm on
fleet-tree file count and churn rate wired into the existing sk-alert 7:15
brief, and phase 1 explicitly measures Syncthing item churn before phase 3
turns on per-service status.

### R3: Control-plane SPOF on .158

Accepted consciously (redundancy mantra noted, machinery rejected). Damage is
bounded by design: sknoded keeps last-known placements running with no control
plane (static-pod behavior), Jobs and Crons on other nodes continue, and
scheduler failover to .41 is a stateless one-command manual runbook. What is
lost during an outage is re-placement and new placement, not running services.

### R4: Actuation flapping and fighting the operator

A reconciler that restarts what Chef just stopped by hand is worse than none.
Mitigations: `cordon` and per-service `spec.paused: true` are first-class from
phase 3 day one; crash-loop backoff with `CrashLooping` condition instead of
restart storms; failover defaults to `manual` with alert (5.2); sknoded on the
local box starts report-only.

### R5: Scope creep toward the machinery

The K8s gravity well is real (leases, webhooks, CRDs, operators). Guardrails:
the non-goals list in section 1 is normative; any card that adds a consensus
mechanism, a network layer, or a second datastore is out of scope for this
epic and needs its own justification; every phase must ship working software
(YAGNI is a gate in review, phase by phase). Right-sized complexity (3.6) is
the same gate from the other side: features wait for a demonstrated need.

### R6: Authenticity of desired state

Single-writer-per-file is integrity by convention, not authenticity: any
process with write access to the Syncthing tree could author spec, and the
cognitive layer (section 8) deliberately adds an AI writer to that tree.
Mitigation: spec and placement writes are SIGNED with capauth/PGP (infra
already in production here), and sknoded VERIFIES the signature before
actuating. Verification matters most at the actuation boundary, so the
enforcement card lands with Phase 3 (where actuation begins), rolled out
permissive-then-enforce. The Phase 1 store carries a writer-identity block
and a verification seam from day one, so signing is a flag flip and key
ceremony, not a migration.

### R7: Cognitive-layer runaway

An AI operator that writes bad spec at machine speed could thrash the fleet.
Bounded by construction: the freeze kill-switch halts all actuation with one
human-owned file; dry-run makes propose the default posture; the approval
gate stops high-blast-radius actions regardless of actor; signed writes make
every change attributable per seat; and the single-writer invariant still
bounds blast radius (the operator seat writes spec, never status, never
placements, never another node's files). The worst uncaught case is bad spec
on low-blast-radius objects, which the autonomic layer applies conservatively
(failover manual by default, crash-loop backoff) and the event log records
for rollback.

## Roadmap

Ordering rationale: the foundational first controller is Node (heartbeat +
capacity + conditions). Everything downstream needs "who is alive and how much
headroom": the scheduler cannot filter, ServiceController cannot place,
autopilot dual-node dispatch cannot pick, and failover cannot trigger without
it. It is also the cheapest to ship honestly because the capacity math already
exists in autoscale.py and the health probes already exist in doctor; phase 1
is 90 percent plumbing of proven parts, and it ships standalone value
(a live `skfleet nodes` fleet view) with near-zero blast radius since it only
ever writes new files in its own subtree. The prior toward Node-first is
confirmed.

Each phase ships working software on its own. No phase is scaffolding.

### Phase 1: Fleet visibility + substrate (Node kind + sknoded self-report + skfleet CLI)

Ships: a live, truthful fleet inventory on every node, plus the substrate
primitives every later phase is born using (events, freeze, writer identity,
join/admission).

- Card 1.1: Fleet object store + conventions + events + freeze + identity seam
  - Kind/controller: substrate (all kinds)
  - Deliverable: `skcapstone/fleet/store.py` with spec/status/placement file
    helpers, generation handling, single-writer path guards, plus the
    `~/.skcapstone/fleet/` tree and object JSON conventions of section 4.
    Also, from day one: the per-node bounded rotating event log of 3.5
    (`emit`/`read`), the freeze flag primitive (`objects/_freeze.json`,
    `is_frozen`), and a writer-identity block on every spec/placement write
    with a verification seam (no-op verifier until the Phase 3 signing card).
  - Acceptance: unit tests prove a writer cannot write outside its ownership
    path; round-trip read merges spec + status; generation bump and
    observedGeneration staleness detection covered; event log rotates at its
    size cap and dedupes repeats inside the window; freeze flag round-trips
    and the store's actuation-guard helper honors it; the single-node-mode
    test proves the 3.6 invariant (one node, zero objects of every other
    kind: no directories demanded, no writes, no errors, cheap listing).
- Card 1.2: sknoded v1 (self-report + join request)
  - Kind/controller: Node / sknoded
  - Deliverable: `sknoded.service` (systemd --user) writing heartbeat.json and
    node.json (capacity via `skharness.autocode.autoscale` as a library,
    conditions via doctor probes) every 60s, in place, write-on-change. When
    no `objects/node/<self>.json` exists, sknoded writes its join request
    (`status/<self>/join.json`, section 9) and keeps self-reporting as
    Pending; it never touches `objects/`.
  - Acceptance: running on .158 and .41 for 48h produces correct capacity and
    Ready conditions, with Syncthing item churn (including events) measured
    and below the agreed cap (baseline captured for R2); on a tree with no
    node object, sknoded produces a valid join request and a path-guard test
    proves it never writes outside `status/<self>/`.
- Card 1.3: NodeController + `skfleet nodes` / `describe` / `cordon` / `explain`
  - Kind/controller: Node / NodeController
  - Deliverable: derived Ready/NotReady/Dead from heartbeat age (Pending for
    unadmitted joiners), cordon flag in Node spec, `skfleet nodes` table
    (name, labels, taints, ready, cpu/ram/disk headroom, age) and `skfleet
    describe node <n>`. Plus the self-describing seam: `skfleet explain
    [kind] [--json]` listing registered kinds, their spec/status fields,
    condition types with meanings, and available actions (Node only at this
    point; the registry grows with each phase).
  - Acceptance: killing sknoded on .41 flips it NotReady within 180s and Dead
    within 300s as seen from .158; cordon round-trips; conflict-file doctor
    probe live and alerting; `skfleet explain node --json` returns a schema
    complete enough for a fresh operator to construct valid commands from.
- Card 1.4: Self-enrollment + admission for all four nodes
  - Kind/controller: Node
  - Deliverable: `skfleet admit <node>` minting `objects/node/<name>.json`
    from a join request (labels/taints supplied at admit time, presets for
    the section 5.1 values), `--bootstrap` for the first node, and an
    optional known-key auto-approve policy. Cold-start runbook per section 9
    (including the .41 travel taint procedure). All four nodes
    (.158/.41/.100/local) enrolled VIA the join+admit flow, local box
    report-only. This replaces hand-written node files entirely.
  - Acceptance: all four nodes enrolled with zero hand-authored object files;
    `skfleet nodes` from any node shows all four with correct labels; .100
    GPU capacity (VRAM) reported; cold-start runbook rehearsed end to end on
    one box.

### Phase 2: Scheduling (scheduler v1 + autopilot dispatch re-home)

Ships: autopilot stops targeting a dead or traveling node; placement is
capacity-aware fleet-wide.

- Card 2.1: Scheduler v1 (filter + least-loaded + placement writes)
  - Kind/controller: scheduler
  - Deliverable: filter (Ready, cordon, selector, `NoSchedule`
    taints/tolerations, headroom) + least-loaded selection with a single
    deterministic tiebreak (lexicographic node name) per section 7, writing
    `placements/`; honors the freeze flag; runs on .158 under skscheduler;
    manual-failover runbook for .41. No preference or affinity scoring in v1.
  - Acceptance: given synthetic Node fixtures, table-driven tests pin the
    placement decisions (gpu selector lands on node-100, cordon excludes,
    least-loaded wins between two eligible nodes, tiebreak deterministic,
    frozen tree produces no writes); idempotent re-run produces no placement
    churn.
- Card 2.1b (DEFERRED, v1.1): preference + affinity scoring
  - Kind/controller: scheduler
  - Deliverable: `PreferNoSchedule` ordering (travel taint deprioritizes
    node-41, interactive taint makes node-local a last resort) and
    label-affinity bonuses, layered on the v1 filter output.
  - Gate: opens only on a demonstrated placement-contention need (a carded
    incident or a measured bad placement). Until then, right-sized
    complexity (3.6) says the v1 selection is enough.
- Card 2.2: Autopilot dispatch via scheduler
  - Kind/controller: Job / JobController (existing autopilot)
  - Deliverable: `skharness.autocode` dispatch consults scheduler placement
    instead of the static .158+.41 list; coord claim remains the execution
    gate; autoscale numbers come from Node status.
  - Acceptance: with .41 cordoned or Dead, `skos autopilot run` places all
    builds on .158 with no config edit; with both Ready, heavy-build cards
    carrying the heavy-build nodeSelector land on .41 (filtering, not
    preference); a stale placement can never double-run a card (claim-race
    test).
- Card 2.3: `skfleet get placements` + placement audit trail
  - Kind/controller: scheduler
  - Deliverable: read path showing current placements with reasons (which
    filter/score decided), logged per decision.
  - Acceptance: every placement visible with a one-line reason; reasons match
    the pinned test table.

### Phase 3: Declarative Services (ServiceController + sknoded actuation)

Ships: the highest visible payoff, self-healing declarative services, starting
with a pilot set.

- Card 3.1: Service kind + sknoded actuation (systemd --user)
  - Kind/controller: Service / sknoded
  - Deliverable: sknoded converge loop driving systemd --user units via the
    trustee verb library; per-unit status files; crash-loop backoff +
    `CrashLooping` condition; `spec.paused` honored.
  - Acceptance: pilot Service `skwhisper@lumina` on .158: `systemctl --user
    stop` is healed within 60s; paused=true stops healing; kill-loop hits
    backoff, raises the condition, and alerts instead of storming.
- Card 3.2: ServiceController + pilot fleet set
  - Kind/controller: Service / ServiceController
  - Deliverable: Service objects for the pilot set (skgateway, skcomms,
    skchat daemon, skwhisper@agent) with selectors and health checks; drift
    between spec and observed raises conditions; failover=manual default with
    sk-alert on node-Dead.
  - Acceptance: all pilot services show Ready in `skfleet get services`; a
    node-Dead simulation produces an alert and no automatic re-place;
    Syncthing churn re-measured against the phase 1 baseline (R2 gate).
- Card 3.3: Docker runtime + drain
  - Kind/controller: Service / sknoded
  - Deliverable: Docker/compose-backed Services (coturn, livekit stack);
    `skfleet drain <node>` = cordon + alert listing what runs there (manual
    move in v1, honoring the conservative failover default).
  - Acceptance: a Docker Service converges like a systemd one; drain on .41
    cordons and enumerates residents correctly.
- Card 3.4: Onboard remaining services + retire hand-run deploys
  - Kind/controller: Service
  - Deliverable: skmemory daemon, ollama, piper-tts, nostr relay as Services
    (skmem-pg explicitly excluded, health-condition only); runbook updates
    pointing at `skfleet` instead of per-box systemctl.
  - Acceptance: `skfleet get services` is a complete and truthful map of
    long-running fleet workloads; one full week with zero manual restart
    interventions on the onboarded set, or each intervention carded as a bug.
- Card 3.5: Signed desired state (hardening, R6)
  - Kind/controller: substrate / sknoded
  - Deliverable: spec and placement writes signed with capauth/PGP under the
    writer's agent identity, filling the Phase 1 writer-identity seam;
    sknoded verifies signatures before actuating and on failure raises a
    `SpecUnverified` condition plus sk-alert (running services untouched, no
    new actuation from unverified spec); trust roster is capauth's existing
    key store; rollout is permissive-then-enforce behind a flag.
  - Acceptance: a tampered or unsigned spec file is refused for actuation
    and alerts within one reconcile period; signed writes verify end to end
    on the pilot set; permissive mode logs but actuates, enforce mode
    refuses, and the flip is a config change with no migration.

### Phase 4: Cron unification (CronController)

Ships: one declarative view and missed-run detection for all scheduled work.

- Card 4.1: CronJob kind + skscheduler adapter
  - Kind/controller: CronJob / CronController (skscheduler wrapped)
  - Deliverable: skscheduler reads CronJob objects alongside its existing
    config; writes run status + `MissedRun` conditions.
  - Acceptance: a fleet-defined CronJob runs on schedule; disabling the node's
    skscheduler surfaces `MissedRun` within one period; existing legacy jobs
    unaffected.
- Card 4.2: Migrate the four standing schedules
  - Kind/controller: CronJob
  - Deliverable: autopilot-daily, backups, digests, 7:15 brief as CronJob
    objects; legacy definitions removed after parity.
  - Acceptance: one week of runs with status visible in `skfleet get
    cronjobs` and zero missed or double runs versus the legacy baseline.

### Phase 5: Agents as objects (AgentController)

Ships: declared soul/model/placement per agent with drift healing.

- Card 5.1: Agent kind + drift detection
  - Kind/controller: Agent / AgentController
  - Deliverable: Agent objects for lumina/opus/jarvis; controller compares
    spec (soul, model, daemon placement) to observed (active.json, model
    routing, daemon Service) and raises `SoulLoaded`/`ModelRoutable`/
    `DaemonReady` conditions.
  - Acceptance: flipping lumina's active soul out-of-band raises a condition
    within 120s; `skfleet get agents` shows the truthful triple (soul, model,
    node) for all agents.
- Card 5.2: Agent convergence (soul + model + daemon)
  - Kind/controller: Agent / AgentController
  - Deliverable: converge mode: controller applies spec via the existing
    toggles (active.json write, model-switching per-agent selection API,
    daemon realized as an owned Service object).
  - Acceptance: editing an Agent spec re-points soul and model with no manual
    steps and daemon follows its selector; converge is a no-op when in sync
    (no write churn).

### Phase 6: Models as objects (ModelController)

Ships: model serving on .100 declared, health-conditioned, and wired to
gateway routing. Explicit no-duplication rule (5.6): skgateway's `sk-default`
auto-router already falls back around dead upstreams; ModelController only
feeds it upstream health and never reimplements routing.

- Card 6.1: ModelServer kind + health conditions
  - Kind/controller: ModelServer / ModelController
  - Deliverable: ModelServer objects for ollama :11434, ornith/qwen3.6 :8082,
    embeddings :11435 on node-100 (tolerating its dedicated taint); sknoded
    probes ports and loaded models; `Serving` conditions.
  - Acceptance: killing the :8082 server shows `Serving=False` within 120s in
    `skfleet get modelservers` and alerts; VRAM budget reported.
- Card 6.2: Gateway upstream sync
  - Kind/controller: ModelServer / ModelController
  - Deliverable: skgateway upstream health fed from ModelServer conditions so
    `sk-default` routing avoids dead upstreams (skgateway remains the routing
    authority; controller only feeds health).
  - Acceptance: with :8082 down, `sk-default` requests succeed via
    skgateway's EXISTING fallback path instead of 502 (verified to exercise
    the existing router, with no routing decision code added to
    ModelController: the controller writes health, the gateway routes);
    recovery restores routing within one reconcile period.

### Phase 7: Config and secrets audit surface (ConfigController)

Ships: fleet-wide presence, drift, and rotation-age visibility without
building secret distribution.

- Card 7.1: Config kind + presence/age conditions
  - Kind/controller: Config / ConfigController
  - Deliverable: Config objects referencing skvault entries by name and
    expected config files by hash; per-node `SecretPresent`, `ConfigDrift`,
    `RotationOverdue` conditions on a 6h tick; no secret material in fleet
    objects (enforced by test).
  - Acceptance: aging a test entry past rotationMaxAgeDays raises
    `RotationOverdue` and alerts; a drifted config hash raises `ConfigDrift`;
    grep-style test proves no secret bytes ever land under `fleet/`.
- Card 7.2: Fleet epic closeout: docs, dashboard card, incident drill
  - Kind/controller: all
  - Deliverable: operator docs (`skfleet` reference, failover runbook, taint
    runbook), an skdashboard fleet card (nodes/services/conditions), and one
    gameday drill: kill .41 mid-build, kill a pilot service, verify alerts,
    healing, and placement behavior end to end.
  - Acceptance: drill completes with every observed behavior matching this
    spec's stated semantics, and deviations carded.

### Phase 8: The operator seat (cognitive layer)

Ships: an AI that manages the fleet through the same surface a human does,
inside hard guardrails. The north-star payoff of the epic (sections 1 and 8).

- Card 8.1: Guardrail surface (dry-run + freeze end-to-end + approval gate)
  - Kind/controller: substrate / skfleet
  - Deliverable: `skfleet apply --dry-run` rendering the predicted effect
    (spec diff, predicted placement changes, actuation the fleet would take)
    with no writes; freeze/unfreeze CLI over `objects/_freeze.json` proven
    end to end (sknoded halts all actuation, services stay up, self-report
    continues, scheduler stops placing); approval gate where
    high-blast-radius actions (fleet-wide restart, drain of a node running
    an always-on service, object delete) park as pending-approval records
    resolved via `skfleet approve` / `skfleet reject`.
  - Acceptance: dry-run of a service move prints the exact placement diff
    later observed on real apply; with freeze on, a full drill produces zero
    actuation actions fleet-wide while services keep running; a drain
    touching an always-on service parks until approved, and expires if not.
- Card 8.2: `skfleet explain` capstone (full self-description)
  - Kind/controller: substrate / skfleet
  - Deliverable: every kind registered with spec/status field descriptions,
    condition meanings, and action list; `--json` schema complete; a cold
    read operator guide generated from it.
  - Acceptance: a fresh agent given only `skfleet explain --json` output
    constructs valid `skfleet` commands for every kind (scripted probe test).
- Card 8.3: AI operator loop v1 (propose, then apply within guardrails)
  - Kind/controller: cognitive layer
  - Deliverable: an operator loop (a scheduled agent session) that reads
    merged objects + conditions + the event log + alerts, proposes spec
    diffs with dry-run output attached, and applies low-blast-radius diffs
    signed under its own agent identity; high-blast-radius always parks at
    the approval gate; the freeze flag always wins.
  - Acceptance: injected fault drills (node death of a pilot service's home,
    a RotationOverdue config entry) produce correct proposed diffs; the
    applied subset converges; flipping freeze mid-drill halts the loop's
    actuation; every write is attributable to the operator seat via
    signature and event log.

Dependency summary: 1 -> 2 -> 3 form the spine (visibility, then placement,
then actuation). 4, 5, 6, 7 each depend only on 1 (plus 3's Service machinery
for the agent daemon part of 5), and can reorder opportunistically if a phase
stalls, but the listed order tracks value: cron and agents are daily-felt,
models and config are audit-grade. Phase 8 is the capstone: it depends on 1
(events, freeze, explain seam, writer identity) and 3 (actuation + signing),
and grows more useful with every other phase, but its guardrail surface
(Card 8.1) can start any time after 3.
