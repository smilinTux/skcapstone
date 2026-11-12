# Work Router Coherence: one admission order for the chi fleet

**Status:** Proposed
**Date:** 2026-09-19
**Authors:** Claude (assessment agent, session 01VB4cQBMLvDaTAFBz5hcUgW), Chef (direction)
**Scope:** the chi estate's two work-assignment systems: the rotation fleet
(`scripts/fleet/skfleet-rotate.py`, timer-driven on chiap01-04 and chiap08) and
the builder-node dispatch path (`src/skcapstone/fleet/builder_dispatch.py`,
consuming on node-ziowk01). Assessment only; nothing here was changed on any
host. All code references are to origin/main at `332ef963`; all live
measurements are from 2026-09-19, 01:49 to 02:05 CDT, taken fresh for this
document.

## 0. Verdict in three sentences

The two systems are not two schedulers fighting over one pool; they are one
scheduler (rotation) plus one governed remote-dispatch channel (builder) whose
exclusivity rule is a one-commit accident. PR #635 (2026-09-11, "make Niobe
builder dispatch reachable") removed every builder-eligible card from every
local lane on every host, unconditionally, so a channel built to feed one idle
Windows box its first four cards became the sole owner of the whole card class,
including cards it has permanently failed and can never run. The fix is not a
merge and not a new router: it is changing the withhold from "eligible for the
builder path" to "actively held by the builder path", plus making builder
capacity data instead of a constant.

## 1. Assessment

### 1.1 What each system is actually for

**System A, the rotation fleet, is the general scheduler.** Verified in
`scripts/fleet/skfleet-rotate.py` at origin/main:

- Hash-partitions the ready pool over `ROTATION_HOSTS` into stable per-host
  ownership (`_pool_v2_owner_map`, line 6359). Capacity is deliberately NOT an
  ownership input; the capacity-weighted partition shipped 2026-09-16 was
  reverted by PR #778 after three consecutive chiap03 cycles partitioned the
  same pool over three different host rosters and every host reported
  `owned=0`. Capacity is honored at selection time instead, through per-host
  lane targets (`SKFLEET_TARGET`, `SKFLEET_{QWEN,KIMI,GLM}_TARGET`).
- Materializes exact pinned source checkouts locally:
  `_source_workspace_spec` (line 648) demands a credential-free https
  repository, a bounded `base_ref`, and an exact 40-hex `base_revision`;
  `_materialize_worker_workspace` (line 869) clones by NAMED ref, verifies the
  revision is an ancestor of `FETCH_HEAD`, detaches onto the exact revision,
  and can safely reset a clean reused workspace (`_verify_source_workspace`,
  line 775, `reset=True`).
- Launches transient systemd workers (`skfleet-worker-<lane>-<cid>.service`),
  with claim fences, churn breaker, claim TTL, and (merged, not yet deployed)
  the wedge watchdog.

**System B, builder dispatch, is a governed remote channel, not a scheduler.**
It was built by PR #609 (2026-09-10, "Activate atomic ZIOWK builder dispatch")
to hand a bounded class of work to standby nodes outside the trusted core,
specifically the idle ziowk01 WSL box. Every design choice in
`builder_dispatch.py` is a trust boundary, not a capability the local lanes
lack: `env -i` with a fixed PATH, a tool allowlist
(`read,bash,edit,write,grep,find,ls`), the `pi-cardstore-guard.mjs` mediated
write extension, credential-free-https-only sources, exact-revision
reconstruction, an idempotent request/status file protocol over Syncthing, and
the rule that the REMOTE node claims the card itself so the CardStore claim
fence stays the single authority.

A card enters the channel via `eligible()` (line 100): exactly one logical
route label among `sk-s|sk-m|sk-l|sk-xl`, plus `source-only`, minus any
`seat-*` label, minus any lane pin (`host-pin`, `codex-only`, `qwen-only`,
`glm-only`).

**The naming is a cross-estate conflation worth recording.** In
`docs/fleet/adr-node-role-model.md`, `builder-standby` is a STATE-TIER role in
Chef's nor estate: a warm full replica and promotion target (node-41) that
"runs almost nothing while holding a complete replica." On chi the same role
string means "remote worker admitted through the governed dispatch channel."
Same word, two architectures. The ADR itself says roles are per-install data,
so this is legal, but anyone reading nor's docs to understand chi's builder
path will conclude the wrong things.

### 1.2 Local lanes ARE capable of builder work (load-bearing claim: verified)

The prior agent's claim holds, on two independent kinds of evidence:

1. **Code.** The local materializer (`_materialize_worker_workspace` plus
   `_verify_source_workspace`) produces the same artifact the remote one does:
   an exact, clean, credential-free checkout at `base_revision`. It is in fact
   STRONGER than the remote one: it fetches the named `base_ref` and verifies
   ancestry, where the remote `materialize_source` (builder_dispatch.py line
   583) does `git fetch --depth=1 origin <40-hex-sha>`, which fails on any
   remote that will not serve an unadvertised SHA and on any binding whose
   revision has been rebased away, and it refuses any pre-existing workspace
   instead of resetting a clean one.
2. **The live fleet.** Card `9968f114` carries `source-only` (no `sk-*` route
   label, so it fails `eligible()` and stays local) and was running in
   chiap01's codex lane as `skfleet-worker-codex-9968f114.service` during this
   assessment. Local lanes run pinned-source work every day; the only thing
   they are forbidden is the subset that ALSO carries an `sk-*` route label.

So the builder path is not "the only place source-bound cards can run." It is
a trust boundary for semi-trusted, intermittently-present hardware.

### 1.3 The incoherence, measured fresh

The withhold (skfleet-rotate.py, the block introduced by PR #635 at commit
`46914a45`) removes every `eligible()` card from every host's owned slice
before lane selection, unconditionally:

```python
_builder_candidate_ids = {candidate[2] for candidate in _builder_candidates}
owned = [candidate for candidate in owned if candidate[2] not in _builder_candidate_ids]
```

Only the host carrying the niobe seat placement (chiap08, per
`~/.skcapstone/coordination/seat-placement.json`) then offers candidates, to
nodes passing `_ready_builders()`: role `builder-standby` AND `actuate: true`.
Exactly one node qualifies (node-ziowk01), with `BUILDER_CAPACITY = 4`
hardcoded (builder_dispatch.py line 36).

One rotate tick on chiap08, 2026-09-19 01:54:55 CDT, journal of
`skfleet-rotate.service`:

```
BUILDER_DISPATCH_IDLE|...|reason=builders-at-capacity: node-ziowk01=4/4   (x13)
BUILDER_DISPATCH_IDLE|...|reason=terminal: node=node-ziowk01 state=failed attempt=2  (x3)
BUILDER_DISPATCH_IDLE|26a806a8|reason=terminal: ... state=blocked attempt=1
BUILDER_DISPATCH_BLOCKED|23554ec7|repository must be credential-free https
SELECTION_EMPTY|chiap08|reason=builder-path-withheld pool=19 owned=0
  owner_free=chiap01:14,chiap02:14,chiap03:12,chiap04:9,chiap08:5 builder_withheld=2
```

Three distinct black holes, all live:

1. **At-capacity parking.** 13+ ready cards idle because the one builder node
   is at 4/4, while `owner_free` advertises 54 free local seats fleet-wide.
2. **Terminal parking, permanent.** A dispatch that reaches
   `state=failed, attempt=2` (`MAX_ATTEMPTS`) is never re-offered;
   `decline_reason()`'s own docstring calls this "parks a card forever under
   its current source binding." The withhold still removes such a card from
   every local lane. Named example: `026a08d9`, folded fresh on chiap08
   (status BACKLOG, labels `source-only, sk-s`), dispatch status
   `failed/attempt=2/retryable=false`, error "existing workspace does not
   match exact source binding." No machine in the estate is allowed to run it.
3. **Invalid-binding parking.** `23554ec7` passes `eligible()` (labels) but
   fails `_source()` (non-https repository), so it can neither be offered
   remotely nor claimed locally.

Note the correction to the working premise: the withheld class is NOT 100% of
the pool fleet-wide. Fresh measurement: pool=19-20, of which roughly 14-16 are
builder candidates. It was 100% of chiap08's OWN hash slice
(`owned=0, builder_withheld=2`), which is what produced the
`SELECTION_EMPTY|reason=builder-path-withheld` reading.

### 1.4 The one qualified builder node mostly fails

Lifetime dispatch record on node-ziowk01 (41 status files under
`~/.skcapstone/fleet/status/node-ziowk01/dispatch/`, read 2026-09-19):

| state | count | dominant error |
|---|---|---|
| completed | 2 | |
| running | 4 | heartbeats fresh (06:57Z) at read time |
| failed | 17 | 14x "exact source reconstruction failed", 1x workspace mismatch |
| blocked | 18 | 16x "unclaimed offer expired", 2x "offered card changed" |

A 5% lifetime completion rate. The failure classes map directly onto the two
robustness gaps named in 1.2 (naked-SHA shallow fetch; refusal to reset a
reused workspace) plus offer leases (900s, `LEASE_SECONDS`) expiring before
ziowk01's consumer picks the request up. The node is alive and working right
now (4 running dispatches with current heartbeats; ziowk01-wsl active on
tailscale with a direct connection), so this is a software-parity problem, not
a dead box.

### 1.5 Is the gateway the real ceiling? Measured: no, with one exception

`http://chiap01:18790/queue` at 2026-09-19T06:56Z:

| backend | active/max | queued | notes |
|---|---|---|---|
| codex | 1/32 | 0 | peakActive 2, 0 errors. All four `SKFLEET_CODEX_MODEL_*` are `sk-codex-mid` |
| zai | 0/10 | 0 | |
| kimi-for-coding | 1/5 | 0 | |
| kimi-k3 | 0/4 | 0 | |
| chiap08-qwen38 | 0/2 | 0 | |
| chiap01-qwen38 | 1/1 | 4 | degraded: errorRate 0.49, 484 timeouts, P50 26.1s |

Pool: totalCapacity 54, utilization 5.5%.

So raising builder throughput does NOT just relocate the queue: the routes
builder work actually uses (sk-codex-mid and kin) have an order of magnitude
of headroom. The exception is the local qwen38 backends; `chiap01-qwen38` at
max=1 is saturated and degraded, and any plan that routes more work at qwen
lanes moves the queue there. Note for the record: the nor-side memory that
"codex max=4 on purpose" does not describe chi; chi's codex admission cap is
32. Estates differ; read the estate's own `/queue`.

### 1.6 Which hosts are what today (all measured 2026-09-19)

| host | cores | RAM (avail) | load1 | rotate timer | lane targets (total/qwen/kimi/glm) | roles beyond lanes |
|---|---|---|---|---|---|---|
| chiap01 | 32 | 61G (25G) | 3.97 | active | 8/1/3/0 | skgateway :18790, qwen38 backend |
| chiap02 | 24 | 31G (13G) | 1.97 | active | 8/1/3/0 | |
| chiap03 | 24 | 31G (21G) | 3.17 | active | 7/0/3/0 | |
| chiap04 | 16 | 27G (20G) | 4.06 | active | 7/0/0/0 | |
| chiap08 | 24 | 31G (16G) | 6.37 | active | 3/0/0/0 | control node, all five seats (niobe/link/mero/atlas/seraph), qwen38 backend |
| ziowk01-wsl | 8 (WSL cap of a 16-core 9950X3D) | 47G (43G) | 0.29 | n/a (sknoded consumer) | 4 builder dispatch slots |
| chiwk12 | 24 threads | 22.7G | offline | n/a | unregistered role; see 1.8 |

All five chiap hosts run the identical installed package
`0.15.168.dev222+gd448c2fa`. That commit IS on origin/main but is 43 commits
behind it; everything merged after #756, including the wedge reaper (#799),
exists in git only and is not deployed anywhere on chi.

Node registry (`~/.skcapstone/fleet/objects/node/`, identical fleet-wide via
Syncthing): `node-chiap08` (control, actuate=false), `node-ziowk01`
(builder-standby, actuate=true, promoted from worker-gpu on 2026-09-11; the
`.pre-builder` file is the pre-promotion backup), `chiwk12` (empty role,
actuate=true). chiap01-04 have no node objects at all, and nothing needs them
to: rotation membership comes from `ROTATION_HOSTS`, not the registry.

### 1.7 The ancient pi processes: census, classification, and reaper scope

Fresh census (`ps -eo pid,ppid,etimes,pcpu,stat,args`), 2026-09-19:

**chiap08:**

| pid | age | %CPU | what it is | verdict |
|---|---|---|---|---|
| 919444 | 25.3d | 0.0 | tmux SERVER for session `sk-parallel-wave2-20260824` (the Aug 24 wave2 integrator launch; its command line carries `--model skgateway/sk-codex`, which is where the "skgateway/sk-codex" label came from) | zombie holder, see below |
| 71361 | 23.6d | 0.0 | `pi` child of 919444: the wave2 integrator for card `793599bf` | **zombie.** Fresh fold: `793599bf` is DONE, unclaimed. Safe to remove by hand |
| 987399 | 12.2d | 0.1 | tmux server, session `sklegal-af427eb5`, which ALSO now hosts the newer `skl-w156-kimi-01` session | card `af427eb5` folds DONE. The Sep 15 kimi worker under the same server may not be done: kill sessions, never the server |
| 3338839 | 14.4d | 0.0 | bare `pi` | zombie, parent unknown; verify before touching |
| 400887 | 13.5d | **11.2** | bare `pi`, actively burning CPU | **unknown, do not touch** until its parent (pid 3315543) and output are identified |
| 1361442/1365364 | 3.7d | 0.0 | `skl-w156-kimi-01` wave worker | stale wave worker, verify card then clean |

**chiap01:** seven `pi` processes all aged 3.67d under one tmux server: the
`skl-w156-{codex-01..05,glm-01..02,kimi-01}` wave launched Sep 15, all at 0.0%
CPU; one more at 3.4d. The premise "chiap01 has two pi processes at 403 hours"
did NOT reproduce: the oldest pi on chiap01 today is 88 hours. (Two empty
tmux sessions from Sep 6, `sk-04e96c18-r4` and `sk-f1d22fec-r2`, are the
likely fossils of whatever was measured earlier; their pi children are gone.)

**None of these is the gateway.** The chi gateway is a node process
(`~/skgateway-runtime-*/src/index.mjs --port 18790` on chiap01, pid 1182122)
and never appears in a `pi` census. No pi cleanup can kill it, though a sloppy
`pkill -f pi` pattern could still match unrelated processes: any manual
cleanup must address exact pids or exact tmux session names.

**PR #799 wedge-reaper scope: these processes are out of it four times over.**

1. Not deployed: chi runs `d448c2fa`, which predates #799.
2. Default off: actuation requires `SKFLEET_WEDGE_MODE=report|enforce`
   (`_wedge_mode`, rotate line 3168); unset on every host.
3. Not enumerated: the progress scan only sees tmux sessions with lane
   prefixes `codex-auto- / glm-auto- / qwen-auto- / kimi-auto- / esc-auto-`
   and `skfleet-worker-*` units. `sk-parallel-wave2-*`, `sklegal-*` and
   `skl-w156-*` match nothing, so they are invisible to it.
4. Not stoppable even if enumerated: `_stop_wedged_unit` (line 3175) validates
   the unit name against `_WORKER_UNIT_RE` and refuses an empty or non-worker
   name, so a tmux-only record can never be stopped by it; enforce mode logs
   `WEDGE_STOP_FAILED ... claim left held` and moves on.

Conclusion: the wedge reaper cannot kill the gateway and also cannot clean up
these zombies. They need a separate, supervised, one-time manual pass
(section 3, phase 0), never an automated one.

### 1.8 What is chiwk12?

A real, distinct, currently-offline machine, and a half-finished registration.
Evidence:

- `join.json` (chiap01, `~/.skcapstone/fleet/status/node-chiwk12/`):
  requested 2026-09-07T23:03:34Z by `capauth:jarvis@skworld.io`, capacity
  24 threads, 22.7G RAM, RTX 5070 Ti Laptop GPU, 762G disk. A gaming laptop.
- Last heartbeat 2026-09-07T23:08:52Z: five minutes of life, then silence.
  Node object generation 5, role never assigned, address empty,
  `actuate: true`.
- Tailscale: `chiwk12` (Windows, 100.106.78.68) and `chiwk12-wsl` (Linux,
  100.120.22.21) both exist and are both currently not connected. `ziowk01`
  / `ziowk01-wsl` are SEPARATE tailscale hosts (100.95.135.102 /
  100.95.152.66, the latter active with a direct connection).

So the premise "a half-finished registration that resolved to ziowk01-wsl" is
half right: half-finished yes, but it did not resolve to ziowk01; it is a
different physical box (RTX 5070 Ti laptop vs 9950X3D desktop) whose
registration was started the week before ziowk01's and abandoned when the
machine went offline. Today it is inert: `_ready_builders()` requires
`role == "builder-standby"`, and its role is empty. But it is a landmine, not
litter: `actuate: true` is already set, so the day anyone fills in the role
the fleet will start writing dispatch requests at a node with no address that
has not heartbeated since September 7.

Recommendation (operator action, not automated): keep the object; either set
`cordoned: true` (or `actuate: false`) until the machine is back, or complete
the registration deliberately when it returns. Do not delete it; the join
record is the only inventory of that hardware.

## 2. Recommendation: fix the boundary; do not merge the systems

The two systems should stay two systems, because they answer different
questions. Rotation answers "which host runs which card"; builder dispatch
answers "how does work cross a trust boundary to hardware that is not in the
trusted core." Merging them (making rotation itself write remote requests, or
making every chiap host a registry node) would couple every scheduling change
to the trust protocol and vice versa. What must become ONE thing is the
admission order: a single, written rule for who may hold a card, with no state
in which nobody may.

### 2.1 The contract (the one-router statement)

1. **The CardStore claim is the only ownership authority.** Both paths already
   obey this; it stays.
2. **A builder-eligible card is withheld from local lanes ONLY while the
   builder path actively holds it**, defined as: a dispatch request exists on
   some ready builder node whose status is non-terminal
   (`completed/blocked/failed@max/stale` are terminal) and whose offer lease
   has not expired without a claim. Eligibility alone withholds nothing.
3. **Terminal or unplaceable means local.** A card whose dispatch is terminal,
   whose binding fails `_source()`, or for which every ready builder is at
   capacity, is an ordinary pool card and follows the hash partition to a
   local lane.
4. **The builder path gets right of first refusal, not ownership.** The niobe
   host offers eligible cards each tick exactly as today; a placed request
   withholds the card (rule 2) for at most one lease window before a remote
   claim must exist. The race between a fresh offer and a local claim is
   resolved by the CardStore fence plus the existing
   `_ensure_request_matches_current_card` re-fold on the consumer; the cost of
   losing the race is one wasted remote materialization, which is the correct
   direction to fail.
5. **Capacity is data.** Per-node builder slots live on the node object; per
   -host lane targets live in the host's systemd environment; neither is a
   module constant.

### 2.2 What this is NOT

- Not work-stealing between chiap hosts. The stable hash partition and the
  2026-08-28 measurement behind it (three hosts claiming one card within
  350ms) are untouched.
- Not a new scheduler, daemon, or seat.
- Not a change to `eligible()`'s trust criteria, the guard extension, the
  tool allowlist, or the claim protocol.
- Not a nor-estate change. Everything in section 3 phases 1-2 is code
  (travels by git, estate-neutral, inert where no builder node exists);
  phases 0 and 3-4 are chi-local operator actions. Nothing touches noroc2027
  or 192.168.0.41.

## 3. Phased plan

Every phase is independently shippable and independently revertible. Commit
before long verification in every phase.

### Phase 0: supervised cleanup and registry hygiene (operator, no code)

- For each zombie in the 1.7 tables: re-fold its card in a fresh process; if
  the card is terminal (DONE/archived/void) and the process is at ~0% CPU,
  `tmux kill-session -t <exact-session>` (never `kill-server`, never a pkill
  pattern). Log each kill against the card. Explicitly EXCLUDED: chiap08 pid
  400887 (11% CPU, unidentified) until someone reads its parent and output.
- chiwk12: cordon or complete, per 1.8.
- Verification: `ps` census shows no pi older than its card's terminal event;
  gateway `/health` unchanged before and after.
- Rollback: none needed (nothing running was doing anything); the cards were
  terminal before the kill by precondition.

### Phase 1: the withhold follows the contract (code, skfleet-rotate.py)

Replace the unconditional `_builder_candidate_ids` subtraction with an
"actively held" set computed per tick from the dispatch tree the rotate host
already reads: requests under `fleet/dispatch/<node>/` for ready builders,
minus those whose paired status is terminal, minus those whose lease expired
unclaimed. Keep the offer loop byte-identical. Log the split:
`builder_withheld=<held>` vs `builder_returned=<n>` so the change is visible
in one grep.

- Verification (chi, after deploy): (a) `SELECTION_EMPTY|reason=builder-path-
  withheld` stops appearing while free local seats exist; (b) named parked
  cards `026a08d9`, `84a113a1`, `23554ec7` reach a local lane claim or a
  triage skip within a bounded number of ticks; (c) ziowk01's 4 running
  dispatches continue undisturbed (their cards are actively held, so still
  withheld); (d) no card acquires two live claims (fold in a fresh process).
- Rollback: revert the commit; behavior returns to the full withhold. No
  state migration in either direction.
- Risk: a burst of long-parked cards hitting local lanes at once. Bounded by
  existing per-host lane targets and the claim ceiling; if it thrashes, the
  churn breaker is already in the claim path.

### Phase 2: builder materializer parity (code, builder_dispatch.py)

Bring `materialize_source` up to the local lanes' standard: fetch the named
`base_ref` and verify `base_revision` ancestry instead of shallow-fetching a
naked SHA; allow detach-reset of a CLEAN pre-existing workspace exactly as
`_verify_source_workspace(reset=True)` does; keep refusing dirty workspaces.
Separately, measure ziowk01's consumer cadence against the 900s offer lease
before touching `LEASE_SECONDS`; 16 of 41 dispatches died as "unclaimed offer
expired" and the fix may be consumer frequency, not lease length.

- Verification: re-offer three representative previously-failed cards (needs
  an operator attempt-reset or a binding bump); target is the failure classes
  "exact source reconstruction failed" and "workspace does not match" going
  to zero on new dispatches; completion rate tracked weekly from the status
  tree (baseline: 2/41).
- Rollback: revert; the request/status protocol is unversioned by this change.
- Note: two other agents are currently working in this file; this phase must
  be coordinated with (or handed to) them, and lands as its own PR.

### Phase 3: capacity as data (code + chi config)

- `BUILDER_CAPACITY` becomes per-node: read `spec.capacity.workers` (or a
  `workers=<n>` label) from the node object, env-overridable
  (`SKFLEET_BUILDER_CAPACITY`), default 4 (today's constant, so deploying the
  code changes nothing until a node object says otherwise).
- ziowk01: hold at 4 until phase 2 lifts the completion rate; then raise
  (the box is at load 0.29 with 43G free; the gateway codex backend it feeds
  has 31 free slots). Chef's "load ziowk01 up" is gated on it stopping
  failing, not on capacity.
- Lane target review AFTER phase 1 has produced a week of unstarved data.
  Directional proposal from today's measurements, not to be applied blind:
  chiap01 8 (room to grow, but it hosts the gateway and the saturated
  chiap01-qwen38 backend, so grow codex/kimi, not qwen), chiap02 8->9
  (lowest load in the fleet), chiap03 7->8, chiap04 7->5 (16 cores at load
  4-5 with 7 lanes is the thinnest box in the rotation), chiap08 3 (control
  plane and five seats; do not raise).
- Verification: `reporting_capacity()` per host tracks the new targets; load1
  stays under cores/2 on every host for a week; chiap01-qwen38 queue depth
  does not grow.
- Rollback: env/config edits, per host, independently.

### Phase 4: write the contract down where the code lives (docs)

- Section 2.1 goes into `docs/fleet/` as the admission-order reference, with
  the estate note from 1.1 (builder-standby means different things in nor and
  chi) so the next reader does not re-derive it.
- Correct the working memory that "codex max=4" applies to chi (it is nor's
  gateway; chi's is 32).

## 4. Premises checked against fresh measurement

| premise given | fresh finding |
|---|---|
| rotation timers active on all five hosts | confirmed (all five, `skfleet-rotate.timer` enabled and firing) |
| pi worker counts 10/9/9/7/4 | not reproducible as workers. Running `skfleet-worker-*` units: chiap01=3, others=0. The larger numbers count raw `pi` processes, most of which are the 1.7 zombies; `pgrep -c pi` also matches unrelated names and should not be used for this |
| chiap01 working 9968f114 (codex) and d13c0a05 (kimi) | confirmed as running units (plus qwen-34115541) |
| 100% of card pool classified builder candidates | not fleet-wide: pool 19-20, candidates ~14-16. It WAS 100% of chiap08's own hash slice, which is what the SELECTION_EMPTY line shows |
| `SELECTION_EMPTY\|builder-path-withheld` + `builders-at-capacity: node-ziowk01=4/4` | confirmed live, same tick, 01:54:55 CDT |
| exactly one node passes `_ready_builders()`; `BUILDER_CAPACITY=4` hardcoded at line 36 | confirmed at origin/main (line 36; uses at 236, 343, 349, 734) |
| chiap01 has two pi processes at 403h | not reproduced; oldest pi on chiap01 is 88h. chiap08's 606h/293h/88h population confirmed (25.3d tmux wave2, 13-14d strays, 3.7d w156 wave) |
| PR #799 reaper default-off, does not touch unit-less workers | confirmed in code, and additionally not deployed: chi runs 43 commits behind origin/main |
| chiwk12 is a half-finished registration that resolved to ziowk01-wsl | half-finished yes; but it is a distinct physical machine (RTX 5070 Ti laptop), offline since 2026-09-07, not an alias of ziowk01 |
| gateway per-backend caps may be the real ceiling | no: 5.5% utilization, codex 1/32. Exception: chiap01-qwen38 (1/1, queue 4, degraded) |

## 5. Could not determine

1. **ziowk01 interior state.** SSH from noroc2027 is refused (the ansible key
   is not authorized there) and no route existed via the chiap hosts in this
   session. Its consumer cadence, WSL git/network condition, and why
   "unclaimed offer expired" happened 16 times are inferred from the status
   files it writes, not observed on the box. Phase 2's lease-vs-cadence
   question needs someone on ziowk01.
2. **chiap08 pid 400887** (13.5d old, 11.2% CPU): whether it is doing useful
   work. Deliberately excluded from the cleanup list until identified.
3. **Why the earlier session measured 403h pi processes on chiap01.** Possibly
   the Sep 6 tmux fossils' children, since died; not recoverable from the
   current process table.
4. **The exact claim-state of the running workers' cards** via `CardStore.fold`
   attributes: the fold object exposes no claim field directly (claims are
   read via claim events elsewhere); folds here were used for status/labels
   only, and the running-unit evidence is systemd's, not the fold's.
5. **Whether any OTHER estate consumes `builder_dispatch`** (Casey-side
   deployments beyond this Syncthing ring). The chi registry says no
   (one qualifying node), but only chi's ring was readable from here.
6. **card_events hygiene** (out of scope but observed): folding on chiap08
   drops 54 unreadable lines in `card_events chiap02.jsonl` and at least two
   prose lines written into `chiap08.jsonl` by a worker. Someone should own
   that; it silently narrows every fold.
