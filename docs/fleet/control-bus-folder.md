# The control-bus folder split, and the ssh-pull fallback

Epic `3bbf39ea`, card `5262c161` (parent `ddb2a02f`). Design note. No code.

## The problem, in one pair of numbers

The fleet store is **368K**. The folder that carries it is **19G**.

Today there is exactly one Syncthing folder holding fleet state:

| field | value |
|---|---|
| folder id | `skcapstone-sync` |
| label | `SKCapstone Sovereign` |
| path | `/home/cbrd21/.skcapstone` |
| type | `sendreceive` |
| shared to | `norap2027`, `ollama-gpu` (.100), `jarvis-laptop` (.41) |

Measured on the control node (noroc2027) 2026-08-14:

| path | size |
|---|---|
| `~/.skcapstone` (the whole shared folder) | 19G |
| `~/.skcapstone/agents/` | 13G |
| `~/.skcapstone/backups/` | 4.4G |
| `~/.skcapstone/skcode/` | 552M |
| `~/.skcapstone/fleet/` (**the entire fleet store**) | **368K** |

Inside that 368K:

| path | size |
|---|---|
| `fleet/objects/` | 168K |
| `fleet/placements/` | 96K |
| `fleet/status/` | 56K |
| `fleet/decisions/` | 28K |
| `fleet/atlas/` | 16K |
| total files | 77 |

So a node cannot receive fleet specs without also receiving every agent
memory, every session, and every backup. The fleet store is 0.002 percent of
what a node must accept to join the control plane. That is the whole case for
the split, and it is the mechanism behind the .100 finding: .100 holds **5.0G
of sovereign state across 29 agent directories** and a `.stfolder` marker,
not because anyone decided a GPU worker should, but because receiving fleet
specs was only available bundled with everything else.

### One important exception: `.stignore` already carves out secrets

The folder is not literally all-or-nothing. `~/.skcapstone/.stignore` opens
with:

```
// Private key material must never leave this node
*.key
*.pem
**/private.*
```

That is why .158 carries **11** `agents/*/capauth/identity/private.asc` while
.100 carries **zero**, despite both sitting in the same `sendreceive` folder.
Verified 2026-08-14: of the 29 `.asc` files on .100, not one contains a PGP
PRIVATE KEY BLOCK. .41 holds 3 (opus, lumina, jarvis), installed locally for
the agents that actually run there rather than received over sync.

This exception matters twice over. It proves scoped exclusion inside this
folder already works, which is evidence for the split rather than against it.
And it is enforced in the right place: Syncthing does not scan or announce
ignored files, so .158 never offers the keys at all. A peer cannot request
what was never announced, so the control does not depend on receivers
behaving correctly.

🔴 **But the rule's own custody is the weak link.** `.stignore` is a local
file that Syncthing does not replicate, is not in git, and is not rendered by
any install path. The three copies agree today only because someone deployed
them by hand, and nothing verifies they still do. If .158's copy lost those
three lines, 11 agent private keys would begin announcing to every peer of
the folder, including the hosted relay noted below, with no alert on that
path. Card `20a1d4d3` makes the ruleset a profile-managed artifact checked by
`node doctor`. Until then, treat the size argument above as the case for the
split and this paragraph as the reason not to lean on `.stignore` as if it
were a guarantee.

Per-node state today:

| node | `~/.skcapstone` | `agents/` | `fleet/` | agent dirs |
|---|---|---|---|---|
| noroc2027 (.158, control) | 19G | 13G | 368K | many |
| .41 (jarvis-laptop) | 6.6G | 3.4G | 368K | 29 |
| .100 (ollama-gpu) | 5.0G | 3.4G | 368K | 29 |

The fleet store is byte-identical everywhere. Everything else is not.

### Finding: an off-fleet device already holds the sovereign folder

.41's copy of `skcapstone-sync` is shared to `sksync.skstack01.douno.it` in
addition to `lumina-noroc2027`. That is a hosted relay, not one of the four
fleet nodes, and it therefore holds agent memories, sessions and secrets
under the same all-or-nothing bundle described above. It is out of scope for
this card, but it belongs on the .41 disposition review (card `67e8c15f`)
because the split does not help a device that is already holding a full copy.

## Target state: a scoped `skfleet-control` folder

| field | value |
|---|---|
| folder id | `skfleet-control` |
| label | `SKFleet Control Bus` |
| path | `~/.skcapstone/fleet` |
| carries | `objects/`, `placements/`, `status/`, `decisions/`, `atlas/` |
| budget | under 10MB (today: 368K, so 27x headroom) |
| shared to | every managed node |

The sovereign folder `skcapstone-sync` stops being the fleet's transport and
becomes what its label already claims: sovereign state, `full-replica` tier
only, shared to the control node and its warm replica and nobody else.

The 10MB budget is not decoration. It is the number card `912d309b` enforces,
and it is what keeps "join the control plane" a decision a node can make
without also accepting an unbounded state liability.

## Share matrix

The two axes are orthogonal. Service role decides what runs on a node; state
tier decides how much sovereign state it carries. Neither is derived from the
other, and this table is where that shows up: `builder-standby` runs almost
nothing while holding everything, and `worker-gpu` runs the most while
holding nothing.

| node | role | state tier | `skcapstone-sync` (19G sovereign) | `skfleet-control` (<10MB) |
|---|---|---|---|---|
| .158 / node-noroc2027 | `control` | `full-replica` | yes (sendreceive) | yes (sendreceive, sole spec writer) |
| .41 / node-41 | `builder-standby` | `full-replica` | yes (sendreceive, warm replica and promotion target) | yes (sendreceive) |
| .100 / ollama-gpu | `worker-gpu` | `control-bus` | **no (unshare, see below)** | yes (receive specs, send own status) |
| norpv1300 | `observer` / unmanaged | `none` | no | no |

norpv1300 is the hypervisor hosting the GPU VM. Putting SK software on the
box that hosts the node is risk for near-zero benefit, so it stays off both
axes. It appears in this table precisely so that "not managed" is a recorded
decision rather than an omission.

### The .100 transition is a removal, not an abstention

.100 is **already in** `skcapstone-sync` and already holds the 5.0G. So the
work is unsharing, not declining to share.

🔴 `~/.skcapstone` on .100 is a live Syncthing folder. A plain `rm -rf` there
**propagates the deletes to .158, .41 and noroc2027**. The order is: unshare
first, verify the folder is gone from the peers' configs, and only then
delete locally. That sequence is card `3118769c` and it is not optional.

## The invariant the transport must preserve

`store.py` enforces single-writer-per-file (spec 3.2) by role. That is the
load-bearing invariant of the whole fleet store, and it is enforced at write
time in one process. **A transport cannot enforce it. A transport can only
avoid breaking it.**

Ownership, from `store.py`:

| path class | sole writer | role check |
|---|---|---|
| `objects/<kind>/<name>.json` | operator seat | `write_spec` rejects any role but `operator` |
| `objects/_*.json` (freeze, plane files) | **human** operator | `set_frozen` / `write_plane_file` reject `agent_seat` |
| `placements/<kind>/<name>.json` | scheduler | `write_placement` rejects any role but `scheduler` |
| `status/<node>/**` | the node named `<node>` | `write_status` rejects `writer.node != node` |

So every path class flows exactly one direction:

| path class | direction | never |
|---|---|---|
| `objects/`, `placements/` | control node **to** every managed node | a managed node must never send these upstream |
| `status/<self>/` | each node **to** the control node | a node must never send another node's status subtree |
| `decisions/`, `atlas/` | control node **to** every managed node | same as objects |

Syncthing's `sendreceive` type does not know any of this. It converges on
last-writer-wins and surfaces disagreement as `.sync-conflict-*` files, which
is why the Node kind already carries a `SyncConflict` condition: a conflict
file under the fleet tree is the observable signature of an ownership bug.
The split does not weaken this. It narrows the blast radius from 19G to 368K
and makes the conflict easier to spot.

## The ssh-pull fallback

For a node that cannot or should not run Syncthing (and for norpv1300 if it
is ever brought partway in), the same content moves over ssh on a timer. The
fallback exists so that "no Syncthing" is not the same as "no fleet".

Shape:

- **Downstream, on the managed node:** `rsync` pulls `objects/`,
  `placements/`, `decisions/` and `atlas/` from the control node, read-only
  at the source, into the node's own fleet root.
- **Upstream, on the control node:** `rsync` pulls `status/<node>/` **from**
  each managed node into `status/<node>/`. The control node pulls; the worker
  never pushes.
- Both directions use `--delete` scoped to the subtree they own and to
  nothing else.

The invariant this must preserve, stated as a rule a script can be checked
against: **a puller may never write into a subtree it does not own.** Two
concrete prohibitions follow, and both are the kind of mistake a one-line
rsync flag makes easy:

1. No pull of `objects/` or `placements/` **from** a managed node. Those are
   control-owned; a reverse sync would let a worker mint specs, which is the
   exact authority `write_spec`'s role check exists to deny.
2. No push of `status/` **into** a managed node, and no pull of
   `status/<other>/` from one. Each node owns its own status subtree and only
   its own. Copying node A's status through node B forges an observation.

`objects/_freeze.json` deserves its own line. It is the human's kill switch,
and `is_frozen()` treats an **unreadable** freeze file as frozen: when in
doubt, halt. A transport that can partially write that file is therefore
fail-safe by construction, but a transport that can *skip* it is not. The
freeze file must be in the pulled set, never in an exclude pattern.

## Precondition, already proven

The split assumes relocating the fleet root relocates the whole fleet. Card
`59f78375` proved it: `tests/fleet/test_root_relocation.py` drives sknoded,
admission, the scheduler, `store.write_spec` and `events.emit` against a
temporary `SKFLEET_ROOT` and asserts every created file lands under it while
the real tree gains nothing. The audit behind it found `.skcapstone` in
exactly one place in the fleet package, `paths.py`, and the test suite now
asserts that stays true.

That is what makes this split reversible rather than a leap.

### One caveat the audit surfaced

`decisions/` and `atlas/` are part of the fleet store and are in the scoped
folder above, but neither is a property on `FleetPaths`. `operator_seat/cli.py`
builds them by joining `paths.root` directly (`_decisions_dir`, and the atlas
brief directory at the publish call site). They relocate correctly today
because they do derive from the root, and `test_root_relocation.py` now
asserts exactly that. But they are the two path classes that a future change
could move out from under `SKFLEET_ROOT` without any existing test noticing,
so they are worth promoting onto `FleetPaths` when card `ee6f522d` decides
where the control-bus root lives.

## Open, handed to sibling cards

- `ee6f522d`: nested-folder conflict. `~/.skcapstone/fleet` sits **inside**
  `~/.skcapstone`, so on .158 and .41, where both folders are wanted, the
  control-bus folder would nest inside the sovereign one. Syncthing treats
  nested shared folders as a configuration error rather than a supported
  layout, so one of the two has to move or be excluded from the other.
  Resolve there, not here. The relocation proof below is what makes moving
  the fleet root a real option rather than a hope.
- `912d309b`: the 10MB budget as an enforced audit rather than a sentence in
  this document.
- `fd381757`: the runbook that creates and shares `skfleet-control` and
  unshares the sovereign folder from .100, in the safe order.
- `67e8c15f`: the `sksync.skstack01.douno.it` share noted above.
