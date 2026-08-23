# Where the control-bus folder is rooted

Epic `3bbf39ea`, card `ee6f522d` (parent `ddb2a02f`). Decision note. No code
changes in this card; the code changes it specifies belong to the cards named
at the end.

Read [control-bus-folder.md](control-bus-folder.md) first. It carries the
measured ground truth (368K of fleet state inside a 19G shared folder) and the
share matrix this note has to make physically possible.

## The question

The design note commits to a scoped Syncthing folder, `skfleet-control`,
rooted at `~/.skcapstone/fleet` and budgeted under 10MB. But `~/.skcapstone`
is itself a live Syncthing folder, `skcapstone-sync`, and on the two nodes
that want both folders (`.158` and `.41`) that would put one folder root
inside another. This note decides where the control-bus root actually lives.

**Decision: the fleet tree moves to `~/.skfleet`, a sibling of
`~/.skcapstone`, and `skfleet-control` is rooted there. The move is made by
changing the default in `paths.py`, not by setting `SKFLEET_ROOT` in the
environment on four machines.** The reasoning, the cost, and the ordered
migration are below.

### One thing I could not verify, stated up front

This card forbids changing Syncthing configuration on any machine, so I did
not stand up an experiment to find out what Syncthing v1.27.2 (the version
installed on `.158`) actually does when you hand it a folder path inside
another folder's path: refuse it at config-validation time, accept it with a
warning, or accept it silently. The design note asserts it is a configuration
error. I could not confirm that assertion, and I am not going to restate it as
if I had.

It turns out not to matter, because the argument against nesting does not
depend on Syncthing rejecting it. The argument is that even if Syncthing
accepts the layout, it does the wrong thing, and the next section is that
argument. If someone later confirms the config API refuses nesting outright,
that only removes the possibility of getting into the state by accident; it
does not change the decision.

## Why nesting is the wrong shape even if the transport permits it

Two Syncthing folders whose roots overlap are two independent replication
domains over the same bytes on disk. Each has its own index, its own scan
schedule, its own peer set, and its own last-writer-wins convergence. That is
not "the files sync twice". It is two convergence processes racing over the
same inodes.

Three consequences follow, and the third is the one that matters.

The first is waste, and it is the least interesting. Every file under
`fleet/` is hashed twice and indexed twice, and `.158` announces the fleet
tree to the sovereign folder's peers and to the control bus's peers
separately. At 368K this is nothing. It is listed only so nobody mistakes it
for the objection.

The second is that the split stops splitting. The entire point of
`skfleet-control` is that a node can receive fleet specs without accepting
19G of agent memory. If `fleet/` is still inside `skcapstone-sync`, then any
node holding the sovereign folder still receives the fleet tree through it,
and the new folder is an addition rather than a replacement. That is
tolerable on `.158` and `.41`, which want both. It is not the failure mode.

The third is the real one. `store.py` enforces single-writer-per-file by
role, and the design note is explicit that a transport cannot enforce that
invariant, only avoid breaking it. Overlapping folders break it. When
`.158` writes `objects/service/foo.json`, that write is a local change in two
folders at once. Each folder propagates it on its own timeline to its own
peers, and each folder sees the other's arriving copy as a remote change to a
file it also has locally modified. The observable result is
`.sync-conflict-*` files under `objects/` and `placements/`, which is
precisely the signature the Node kind's `SyncConflict` condition exists to
report as an ownership bug. The control plane would be manufacturing, through
its transport, the exact alarm that is supposed to mean its authority model
has been violated. An alarm that fires for a benign structural reason stops
being an alarm, and this one guards the freeze file's neighbourhood.

So nesting is out on mechanism, not on a rule.

## Option 2: keep the path, exclude `fleet/` from the sovereign `.stignore`

This is the cheap fix and it deserves a fair hearing, because it is one line
in one file and it changes no code. Add `/fleet` to `~/.skcapstone/.stignore`
on `.158` and `.41`, and the two folders no longer overlap in content even
though they still overlap in path. Syncthing does not scan or announce
ignored files, so the sovereign folder would stop carrying the fleet tree and
the third consequence above goes away.

It fails for three reasons, in increasing order of severity.

**The rule has no custody.** `.stignore` is a local file. Syncthing does not
replicate it, it is not in git, and no install path renders it. The design
note already flags this as the weak link behind the private-key exclusion,
and card `20a1d4d3` exists to make the ruleset a managed artifact. Under
option 2, that same unmanaged file becomes the only thing standing between
the fleet store and the two-convergence-domains failure above. Worse, the
exclusion has to hold on `.158` **and** `.41` simultaneously and permanently.
A single node losing the line does not produce a local problem; it produces a
fleet-wide conflict storm sourced from that node. Even with `20a1d4d3`
landed, the guarantee is "a doctor check notices within one poll", not
"cannot happen".

**It does not survive re-adding a node.** The share matrix is not frozen.
`.41` is a promotion target, and any future node that is given the sovereign
folder inherits the overlap on day one, correct only if whoever provisioned
it also hand-placed the ignore line. That is a provisioning step that fails
silently and is only observable as conflict files days later.

**It collides head-on with the `.100` teardown, which is the crux.** The
asymmetry in the share matrix is that `.158` and `.41` want both folders and
`.100` wants only the control bus. Under option 2, the control-bus root on
`.100` is `~/.skcapstone/fleet`, which is inside `~/.skcapstone`, which is the
directory the `fd381757` runbook exists to remove from that node. The
teardown step would have to become "delete everything under `~/.skcapstone`
except `fleet/`", executed against a 5.0G tree with 29 agent directories,
next to a live Syncthing folder where a mis-scoped delete propagates to three
peers. Making the single most dangerous step in the whole campaign into a
selective delete, in order to save a path change, is a bad trade.

Under option 1 that same step is `rm -rf ~/.skcapstone`, run after the
unshare, with nothing inside it that anything still needs. The asymmetry
stops being a filesystem problem and becomes what it should have been all
along: a difference in two folders' share lists, which is the only kind of
asymmetry Syncthing expresses well.

## Option 1, and the refinement that makes it the answer

Relocating is real rather than hopeful because card `59f78375` proved it.
`tests/fleet/test_root_relocation.py` points `SKFLEET_ROOT` at a temp
directory, drives sknoded, admission, the scheduler, `store.write_spec` and
`events.emit`, and asserts that every created file lands under the new root
while the live tree gains nothing. It also asserts, in
`test_only_paths_py_may_name_the_fleet_root`, that `paths.py` is the only
module in the fleet package naming `.skcapstone/fleet`. Verified against the
tree: `src/skcapstone/fleet/paths.py:77` is the single hit.

But "relocatable via an environment variable" and "relocated in production"
are different claims, and the gap between them is where this would go wrong.
`SKFLEET_ROOT` was built as a test override. Eighteen test modules set it,
every one of them by injecting it into a test process (`monkeypatch.setenv`,
or an `env` dict handed to a `CliRunner`), and its own docstring in `paths.py`
calls it the "override for tests". Nothing in production sets it: a repo-wide
grep finds it only in `tests/`, in `paths.py` itself, and in two planning
documents. `systemd/sknoded.service` has no `Environment=` line at all; it is
six lines and an `ExecStart`. To relocate by environment variable you would
have to get `SKFLEET_ROOT` into every execution context that touches the
fleet tree on every node: the sknoded unit, the operator seat loop, each
controller, cron wrappers, and interactive shells where a human runs
`skfleet`.

The failure mode of getting that wrong is what disqualifies the approach. A
process that misses the variable does not error. It silently uses the default
and creates a **second, empty fleet tree** at the old path. `sknoded` would
write heartbeats there, the control node would read the new root, see no
heartbeat, and age the node out as stale. Nothing in the system reports "your
fleet root is wrong"; it reports a healthy-looking node going NotReady. That
is a diagnosis that costs hours and looks like a network problem.

**So: change the default in `paths.py` to `~/.skfleet` and keep
`SKFLEET_ROOT` as the override it was written to be.** Correctness then rides
on which version of the package is installed, which the fleet already manages,
already reports through `node doctor`, and already knows how to roll. It does
not ride on shell and unit environments, which the fleet does not manage at
all. A node on the wrong package version is a legible, single-cause,
version-shaped problem instead of an invisible environment-shaped one.

The name `~/.skfleet` matches what already exists around it: the `SKFLEET_*`
environment prefix, the `skfleet` console script, and the `skfleet-control`
folder id. A sibling of `~/.skcapstone` rather than a child of it, which is
the entire point.

### The symlink idea, rejected

The tempting shortcut is to leave `~/.skcapstone/fleet` in place as a symlink
to `~/.skfleet` so no code changes at all. Do not. Syncthing replicates
symlinks as symlinks rather than following them, and a symlink inside
`skcapstone-sync` would propagate to every peer of that folder, including
`norap2027` and the hosted relay on `.41`, pointing at a path those machines
may not have. It converts a local convenience into a fleet-wide artifact. The
whole reason the relocation is safe is that the two trees are ordinary
directories on ordinary disks.

## The two path classes a move could strand

`decisions/` and `atlas/` are part of the fleet store, are inside the
control-bus scope contract (`control_bus_audit.KNOWN_CLASSES` names all five:
`objects`, `placements`, `status`, `decisions`, `atlas`), and are **not**
properties on `FleetPaths`. `FleetPaths` defines exactly three:
`objects`, `placements`, `status`. The other two are built by joining
`paths.root` at the call site in `src/skcapstone/operator_seat/cli.py`:

- `_decisions_dir(paths)` at line 49 returns `str(paths.root / "decisions")`.
- The atlas brief directory is built inline at line 209 as
  `str(paths.root / "atlas" / "brief")`.

They relocate correctly today, and
`test_the_two_path_classes_outside_fleetpaths_still_relocate` pins both
shapes. Say it plainly anyway: **these are the two path classes a future
change could move out from under the fleet root without `FleetPaths`
noticing**, because they are string joins at a call site rather than a
property on the one object that owns the layout. The scope contract already
disagrees with the path builder about what the fleet store contains, and that
disagreement is exactly the seam a regression slips through.

There is also a live escape hatch for one of them. `operator seat run` takes
`--publish-dir`, and line 209 reads `publish_dir or str(paths.root / "atlas" /
"brief")`. An explicit `--publish-dir` does **not** follow the root. If any
deployed unit, cron entry or wrapper on any node passes an absolute
`--publish-dir` under `~/.skcapstone/fleet/atlas/brief`, that path survives
the relocation and strands the atlas brief on the old root, and it does so
without failing. Grepping for it is a pre-flight step in the migration below.
Nothing in this repo passes it outside tests; the deployed units are not in
this repo, so the check has to run on the nodes.

**Therefore this decision includes: promote `decisions` and `atlas` onto
`FleetPaths` as properties, and route `operator_seat/cli.py` through them,
before the flip.** It is a small change and it closes the gap between the five
classes the audit enforces and the three the path object defines.

## Migration, in order

Three phases. Phase A is additive and cannot break anything. Phase B is the
flip and has a genuine window. Phase C is cleanup and must not start until
Phase B has soaked.

`.158` is the control node (`noroc2027`), `.41` is `jarvis-laptop`, `.100` is
`ollama-gpu`. All three device names verified against the live Syncthing
config on `.158`.

### Phase 0: pre-flight, on every node, before anything moves

Confirm no deployed unit or cron entry passes an absolute `--publish-dir`, and
that nothing sets `SKFLEET_ROOT` already:

```
grep -rn 'publish-dir\|SKFLEET_ROOT' ~/.config/systemd/user/ /etc/systemd/system/ 2>/dev/null
systemctl --user show-environment | grep -i skfleet
```

Both should be empty. A hit here is a stranding path and must be fixed before
Phase B, not during it.

Revert: none, this reads only.

### Phase A: stand up the new tree and the new transport, with nothing reading it

**A1. Copy the tree on `.158`.** `cp -a ~/.skcapstone/fleet/. ~/.skfleet/`.
A copy, not a move. Two identical trees now exist and nothing reads the new
one.
Verify: `diff -r ~/.skcapstone/fleet ~/.skfleet` prints nothing, and
`SKFLEET_ROOT=~/.skfleet ~/.skenv/bin/skfleet control-bus audit` exits 0 with
all five classes present and no out-of-scope paths.
Revert: `rm -rf ~/.skfleet`. Nothing references it.

**A2. Write the control-bus `.stignore` before the folder exists.** On each
node: `~/.skenv/bin/skfleet control-bus audit --stignore > ~/.skfleet/.stignore`.
Before, not after, so the folder's very first scan already has its rules. The
generated ruleset is a whitelist (keep the five classes and `.stfolder`,
ignore everything else at the root), which is stronger than the sovereign
folder's blocklist and is the reason the private-key question does not
reappear in the new folder: nothing but fleet state is ever kept.
Verify: `cat ~/.skfleet/.stignore` and confirm the five `!/<class>` pairs and
the trailing `/*`.
Revert: `rm ~/.skfleet/.stignore`.

**A3. Add the `skfleet-control` folder on `.158`, shared to nobody.** Id
`skfleet-control`, label `SKFleet Control Bus`, path `~/.skfleet`, type
`sendreceive`.
Verify: `syncthing cli config folders skfleet-control dump-json` shows the
path and an empty device list, and the GUI reports Up to Date.
Revert: `syncthing cli config folders skfleet-control delete`.

**A4. Repeat A2 and A3 on `.41` and `.100`** (each with its own local
`~/.skfleet` and its own `.stignore`), then share the folder between all
three. Let it converge.
Verify on each: `diff -r` against `.158`'s copy is empty, and
`~/.skenv/bin/skfleet control-bus audit` exits 0. On `.100` this is the first
time it holds fleet state outside the 5.0G bundle.
Revert: delete the folder on each node. The local `~/.skfleet` directories are
inert and can stay or be removed.

**Halfway through Phase A nothing breaks.** No process reads `~/.skfleet`; the
live fleet is still `~/.skcapstone/fleet` on every node, still transported by
`skcapstone-sync`, still complete. A node with the new folder and no content
just has an empty directory nobody consults. This is why the copy comes before
the flip.

### Phase B: flip the writers

**B1. Freeze.** `~/.skenv/bin/skfleet freeze --reason "control-bus root
relocation"` (or `operator freeze`), written into the old root while it is
still the live one. `is_frozen()` treats an unreadable freeze file as frozen,
so this fails safe, and a frozen plane means no scheduler or controller writes
race the flip.
Verify: `operator status` reports `FROZEN (Atlas stands down)` on every node.
Revert: `skfleet unfreeze`.

**B2. Stop the fleet daemons on all three nodes** (`sknoded`, the operator
seat loop, the controllers).
Verify: `systemctl --user status sknoded` inactive on each.
Revert: start them again.

**B3. Re-sync on `.158`:** `rsync -a --delete ~/.skcapstone/fleet/ ~/.skfleet/`
to pick up everything written since A1, then wait for `skfleet-control` to
report Up to Date on all three nodes.
Verify: `diff -r ~/.skcapstone/fleet ~/.skfleet` empty on every node.
Revert: nothing to revert; this is idempotent and the source is untouched.

**B4. Install the package version whose `paths.py` default is `~/.skfleet` on
all three nodes**, together, not staggered.
Verify: `SKFLEET_ROOT= ~/.skenv/bin/python -c "from skcapstone.fleet.paths
import default_paths; print(default_paths().root)"` prints the new path on
each.
Revert: reinstall the previous version.

**B5. Start the daemons and confirm the writers moved.**
Verify: `~/.skfleet/status/<node>/heartbeat.json` has a fresh mtime on each
node, and `~/.skcapstone/fleet` gains no new files (`find
~/.skcapstone/fleet -newermt '-10 minutes'` prints nothing).
Revert: stop, reinstall the old version, `rsync -a ~/.skfleet/
~/.skcapstone/fleet/` to carry back anything written in the window, start.

**B6. Unfreeze**, and let it run.
Verify: `skfleet node doctor --all` clean, no `.sync-conflict-*` anywhere under
`~/.skfleet`.

**Halfway through Phase B is the state to be afraid of, and it has a name.**
Between B4 on one node and B4 on the next, a node still running the old
package writes `status/<self>/heartbeat.json` into `~/.skcapstone/fleet`,
which still replicates through `skcapstone-sync` to `.158`'s old tree, while
`.158` on the new package is reading `~/.skfleet`. The heartbeat ages. The
node goes NotReady. If the plane were not frozen, the scheduler would react to
a node that is in fact perfectly healthy, and it would react by moving
workloads. That is the entire reason B1 comes before B4.

The correct response to being stuck halfway is to **finish** B4 on the
remaining nodes, not to roll individual nodes back and forth. The revert is a
whole-fleet revert, and it is cheap because the old tree is complete up to the
moment of the flip and both trees sit on the same disk on each node, so
carrying writes back is a local `rsync` rather than a resync over the network.

### Phase C: cleanup, after a soak

**C1. On `.158` and `.41`, delete `~/.skcapstone/fleet`.**
🔴 This is a delete inside a live Syncthing folder, so it propagates to every
peer of `skcapstone-sync`, including `norap2027` and, from `.41`, the hosted
relay `sksync.skstack01.douno.it`. Here that is intended: the whole point is
that the fleet tree stops living in the sovereign folder. But it must be a
deliberate act, and it must come after **every** node is on the new root,
because otherwise it deletes the live tree out from under any node still
running old code.
Verify: `find ~/.skcapstone -maxdepth 1 -name fleet` empty on all peers.
Revert: `rsync -a ~/.skfleet/ ~/.skcapstone/fleet/` on `.158`. The tree is
368K and `~/.skfleet` is a full copy, which is why this delete is recoverable
at all.

**C2. On `.100`, the sovereign unshare and teardown.** That is card
`3118769c` and the runbook in
[runbook-skfleet-control.md](runbook-skfleet-control.md). Under this decision
it is a wholesale `rm -rf ~/.skcapstone` after the unshare, with one
exception noted there: `skmeter` writes `~/.skcapstone/skmeter/<node>-state.json`
and does **not** honor `SKFLEET_ROOT` (it is a recorded exemption in
`test_root_relocation.py` precisely because it is a sibling of the fleet tree,
not fleet state). If skmeter runs on `.100`, preserve that file first or accept
that the joule counter rebaselines.

## What this decision costs

It is a code change plus a fleet-wide package roll, where option 2 was one
line in one file. That is the honest trade: option 2 is cheaper today and
leaves an unmanaged local file as the only thing preventing a conflict storm,
on the exact tree whose conflict files are supposed to mean something else.
This costs a coordinated roll once and then stops being a thing anyone has to
remember.

Three concrete follow-on edits it implies, so the implementing card does not
discover them at test time:

1. `test_only_paths_py_may_name_the_fleet_root` collects fleet-package modules
   containing the literal `.skcapstone/fleet` and asserts the set is exactly
   `{"paths.py"}`. Once `paths.py` names `~/.skfleet` instead, that set becomes
   empty and the test **fails**. It must be retargeted to the new literal. This
   is the test doing its job, and it should be updated rather than relaxed.
2. `_SKCAPSTONE_PATH_EXEMPT` in the same module keeps `skmeter.py` and can drop
   `paths.py`, since `paths.py` will no longer name a `.skcapstone` path at all.
3. `FleetPaths` gains `decisions` and `atlas` properties, and
   `operator_seat/cli.py` uses them instead of joining `paths.root` by hand.

## What it does not solve

The sovereign folder is still 19G, still shared to `norap2027`, and still
shared from `.41` to `sksync.skstack01.douno.it`, which is not a fleet node.
Relocating the fleet tree does not help a device that already holds a full
copy of everything else. That remains card `67e8c15f`.

And the sovereign `.stignore` is still the only thing keeping 11 agent private
keys off the wire, still local, still unreplicated, still unverified. This
decision removes the fleet store's dependence on it. It does not remove the
key material's dependence on it. Card `20a1d4d3`.

## Handed on

- `fd381757`: the runbook that creates and shares `skfleet-control` and
  unshares the sovereign folder from `.100`, in the safe order.
- A new card for the code: change the `paths.py` default to `~/.skfleet`,
  promote `decisions` and `atlas` onto `FleetPaths`, and retarget the two
  assertions listed above.
- `912d309b`: the 10MB budget, already enforced by `skfleet control-bus
  audit`, which is the verification step this migration leans on throughout.
- `20a1d4d3`: the managed `.stignore` ruleset.
- `67e8c15f`: the off-fleet share on `.41`.
