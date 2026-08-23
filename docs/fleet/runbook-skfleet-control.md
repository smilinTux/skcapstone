# Runbook: stand up `skfleet-control`, take `.100` off the sovereign folder

> 🔴 **The fleet is split across a Syncthing MAJOR version boundary. Verified
> 2026-08-16:**
>
> | node | version | config path | database |
> |---|---|---|---|
> | `.158` | v1.27.2-ds4 (Debian) | `~/.local/state/syncthing/` | LevelDB |
> | `.100` | v1.27.2-ds4 (Debian) | `~/.local/state/syncthing/` | LevelDB |
> | `.41` | **v2.1.2** (upstream) | `~/.config/syncthing/` | SQLite |
>
> Every `syncthing cli` invocation below was checked against the **v1.27.2**
> binary on `.158`. The 1.x and 2.x command surfaces are not guaranteed to
> match, and the config file is not even in the same place on `.41`. So before
> running any step of this runbook ON `.41`, re-check that step's syntax against
> the local binary with `syncthing cli --help`, and expect the config path to
> differ.
>
> This is not a blocker for the split, and it is not this runbook's to fix. It is
> a reason not to copy and paste a command from a `.158` step into a `.41` shell
> and assume it did the same thing.


Epic `3bbf39ea`, card `fd381757` (parent `ddb2a02f`). Operational runbook.

Background and measurements: [control-bus-folder.md](control-bus-folder.md).
Where the folder is rooted and why: [control-bus-nesting.md](control-bus-nesting.md).

This runbook has two halves that must be done in this order:

1. **Part 1**, additive: create `skfleet-control` and share it to `.158`,
   `.41` and `.100`. Nothing is removed. Fully reversible at every step.
2. **Part 2**, subtractive: unshare `skcapstone-sync` from `.100` and then
   delete `~/.skcapstone` there. This is the dangerous half.

Do not start Part 2 until Part 1 is verified converged. `.100` must be
receiving fleet specs through the new folder before it stops receiving them
through the old one, or it drops out of the control plane during the gap.

---

## 🔴 The landmine, read this before touching `.100`

**`~/.skcapstone` on `.100` is a live Syncthing folder, `skcapstone-sync`,
shared to `.158` (`noroc2027`), `.41` (`jarvis-laptop`) and `norap2027`. A
plain `rm -rf ~/.skcapstone` on `.100` is not a local cleanup. It is a
fleet-wide delete. Syncthing will faithfully replicate every one of those
deletions to all three peers, and `.158` holds the only full copy of the
sovereign tree: 19G, 13G of it agent memory, plus 4.4G of backups.**

**The order is: unshare first, VERIFY the share is gone from the peers'
configs and from `.100`'s own config, and only then delete locally. Never the
other way round. There is no step in this runbook where `rm -rf` and an
active share coexist, and if you find yourself about to create one, stop.**

The device identity that makes the unshare possible lives at
`~/.local/state/syncthing/key.pem` on `.100`. Do not delete it before the
unshare completes; it is the only orderly way out. See
[dot100-secret-audit.md](dot100-secret-audit.md).

---

## Preconditions

- The nesting decision in [control-bus-nesting.md](control-bus-nesting.md) is
  implemented: the fleet root is `~/.skfleet`, the `paths.py` default has been
  changed, and Phase B of that note's migration has completed and soaked.
  **This runbook assumes `~/.skfleet` is the fleet root on all three nodes.**
  If the root is still `~/.skcapstone/fleet`, stop: Part 2 would delete the
  control-bus folder's own root out from under `.100`.
- All three nodes on the same skcapstone package version.
- You can reach `.158`, `.41`, `.100` and `norap2027`. `norap2027` matters
  because it is a peer of `skcapstone-sync` and Part 2 needs its config
  verified too. If it is offline, Part 2 waits.

Node and device names, verified against the live Syncthing config on `.158`
on 2026-08-16:

| node | Syncthing device name | role |
|---|---|---|
| `.158` | (local, `noroc2027`) | control |
| `.41` | `jarvis-laptop` | builder-standby |
| `.100` | `ollama-gpu` | worker-gpu |
| (Chef's laptop) | `norap2027` | peer of `skcapstone-sync`, not a fleet node |

Full device IDs are in each node's config; get them with
`syncthing cli config devices list`. This document deliberately does not
hardcode them, because a stale ID copied out of a doc is how you unshare the
wrong device.

Every `syncthing cli` command shape below was checked against the installed
binary on `.158` (v1.27.2-ds4 "Gold Grasshopper") by inspecting its help
output. No configuration was read into or written out of any node in the
course of writing this runbook.

---

## ⚠️ `.stignore` does not replicate, so the new folder starts with nothing

Syncthing does **not** sync `.stignore`. It is a local file on each node.

That matters here more than it looks. The sovereign folder's ruleset opens
with

```
*.key
*.pem
**/private.*
```

and that is the only reason `.158` holds 11 agent private keys while `.100`
holds zero, despite both being in the same `sendreceive` folder. **Those rules
do not inherit into `skfleet-control`.** A new folder created without its own
`.stignore` has no exclusions at all.

The fleet tree contains no key material today, so this is not an immediate
leak. It is a property that has to be established deliberately rather than
assumed, on each of the three nodes separately, and it has to be in place
before the folder's first scan, because Syncthing announces what it has
already indexed.

`skfleet control-bus audit --stignore` generates the ruleset. It is a
whitelist, not a blocklist: it keeps the five known path classes and
`.stfolder` and ignores everything else at the folder root. That is a stronger
shape than the sovereign folder's blocklist, because a file type nobody
anticipated is excluded by default rather than included by default. It also
deliberately excludes nothing *inside* those five classes, because
`objects/_freeze.json` is the human kill switch and a transport that can skip
it is not fail-safe.

Making this ruleset a managed, verified artifact rather than three hand-placed
copies is card `20a1d4d3`. Until that lands, step A2 below is a manual step
whose correctness nothing checks afterwards. Treat it accordingly.

## ⚠️ `.41` shares the sovereign folder off-fleet. Flag only, do not act.

`.41`'s copy of `skcapstone-sync` is shared to `sksync.skstack01.douno.it` in
addition to `lumina-noroc2027`. That is a hosted relay, not one of the four
fleet nodes, and it therefore holds agent memories, sessions and whatever the
`.stignore` on `.41` does not exclude.

**This runbook does not touch it.** It is recorded here for two reasons.
First, so that when you run the Part 2 verification and see a device on `.41`
that is not in the table above, you know it is expected and not a surprise
mid-procedure. Second, because any delete that propagates from `.158` or `.41`
reaches it too, which widens the blast radius of a mistake beyond the fleet.
Disposition is card `67e8c15f`.

---

# Part 1: create and share `skfleet-control`

Additive. Every step reverts by undoing exactly that step.

### A1. Seed `~/.skfleet` on `.158`

Only needed if the nesting migration has not already left the tree there. If
`~/.skfleet` is already the live root, skip to A2.

```
cp -a ~/.skcapstone/fleet/. ~/.skfleet/
```

Verify:
```
diff -r ~/.skcapstone/fleet ~/.skfleet && echo IDENTICAL
```

Revert: `rm -rf ~/.skfleet`. Nothing references it yet.

### A2. Write `.stignore` on each of `.158`, `.41`, `.100`, before the folder exists

On each node:
```
mkdir -p ~/.skfleet
~/.skenv/bin/skfleet control-bus audit --stignore > ~/.skfleet/.stignore
```

Before the folder is added, not after. Syncthing announces what it has already
scanned, and a rule added after the first scan does not un-announce anything.

Verify (on each node):
```
grep -c '^!/' ~/.skfleet/.stignore    # expect 12: five classes x2, plus .stfolder x2
tail -1 ~/.skfleet/.stignore          # expect: /*
```

Revert: `rm ~/.skfleet/.stignore`.

### A3. Add the folder on `.158`, shared to nobody

Id `skfleet-control`, label `SKFleet Control Bus`, path `~/.skfleet`, type
`sendreceive`. Add it through the GUI or `syncthing cli config folders add-json`.
Leave the device list empty for now, so the first scan happens before anything
is announced.

Verify:
```
syncthing cli config folders skfleet-control dump-json
```
Path is `~/.skfleet`, type is `sendreceive`, device list is empty. The GUI
should reach "Up to Date" with the file count matching the fleet tree.

```
~/.skenv/bin/skfleet control-bus audit
```
Exits 0. All five path classes present, no out-of-scope files, total well
under the 10MB budget (368K as measured on 2026-08-14).

Revert: `syncthing cli config folders skfleet-control delete`.

### A4. Add the folder on `.41` and `.100`

Same id, same label, path `~/.skfleet` on each, `sendreceive`. On `.100` this
is a new empty directory; do not copy the tree in by hand, let Syncthing fill
it, because that is the thing being tested.

Verify on each: the folder exists and is idle.
```
syncthing cli config folders skfleet-control dump-json
```

Revert: delete the folder on that node. `~/.skfleet` is inert until shared.

### A5. Share the folder between all three

Add `jarvis-laptop` and `ollama-gpu` to `skfleet-control` on `.158`, and add
`.158`'s device to the folder on `.41` and `.100`. Accept the pending folder
offers.

Do **not** add `norap2027`. It is not a fleet node and this is the folder that
gets shared to managed nodes only.

Verify, on each of the three:
```
syncthing cli config folders skfleet-control devices list
~/.skenv/bin/skfleet control-bus audit
diff -r <(cd ~/.skfleet && find . -type f | sort) <(ssh <.158> 'cd ~/.skfleet && find . -type f | sort')
```
The audit is the real gate: it exits 0 only when the tree is inside budget and
inside the scope contract, which is exactly the two properties the share is
supposed to preserve. Do not invent a different check; this one is the
contract.

Revert: remove the device from the folder on each side. The content already
received stays on disk locally and is harmless.

### A6. Soak

Let it run long enough to see at least one real spec change propagate from
`.158` and at least one heartbeat propagate from `.100`.

Verify:
```
# on .158
find ~/.skfleet/status/ -name heartbeat.json -newermt '-5 minutes'
# expect a fresh heartbeat for every managed node, including .100

# on every node
find ~/.skfleet -name '.sync-conflict-*'
# expect nothing. A conflict file under the fleet tree means an ownership bug,
# not a transport hiccup. Stop and diagnose before Part 2.
```

**Part 1 is not done until `.100`'s heartbeat is arriving at `.158` through
`skfleet-control`.** That is the precondition for Part 2, because it is the
evidence that `.100` no longer needs the sovereign folder to stay in the
control plane.

---

# Part 2: take `.100` off `skcapstone-sync`

Subtractive. Re-read the landmine section above before starting.

### B1. Pause the folder on `.100`

The cheapest possible belt, and instantly reversible. A paused folder sends
and receives nothing, so nothing that happens under `~/.skcapstone` on `.100`
from this moment on can reach a peer, whatever else goes wrong later.

```
# on .100
syncthing cli config folders skcapstone-sync paused set true
```

Verify:
```
syncthing cli config folders skcapstone-sync paused get     # expect true
```
The GUI on `.158` should show `.100` as no longer syncing that folder.

Revert: set `paused` back to `false`.

### B2. Remove `ollama-gpu` from the folder on each peer

On `.158`, on `.41`, and on `norap2027`. All three. This is the actual unshare,
and doing it on the peers first means that even if `.100` were somehow
unpaused, its index updates would be rejected rather than applied.

```
# on each peer, get the ollama-gpu device id first
syncthing cli config devices list
syncthing cli config folders skcapstone-sync devices <ollama-gpu-device-id> delete
```

Verify, on each peer:
```
syncthing cli config folders skcapstone-sync dump-json | grep -i <ollama-gpu-device-id>
# expect NO output
```

On `.41` you will also see `sksync.skstack01.douno.it` in that folder's device
list. Leave it. It is card `67e8c15f`, flagged above, and touching it here
turns a scoped runbook into an unscoped one.

Revert: re-add the device to the folder on that peer.

**If `norap2027` is unreachable, stop here.** Do not proceed to B4. An
unverified peer is a peer that might still accept a delete stream. Wait for it.

### B3. Delete the folder from `.100`'s own config

```
# on .100
syncthing cli config folders skcapstone-sync delete
```

This removes the folder from the config. It does not touch the files on disk.

Verify:
```
syncthing cli config folders list
# expect: skfleet-control present, skcapstone-sync ABSENT
```

Revert: re-add the folder pointing at `~/.skcapstone` and re-share it from the
peers. Everything is still on disk, so this is a full recovery as long as B4
has not run.

### B4. Verify, then verify again, then delete

This is the point of no return, and the two verifications are the whole reason
this runbook exists. Run all of them and read every output before typing the
delete.

```
# on .158, .41 and norap2027: .100 is not a device on the sovereign folder
syncthing cli config folders skcapstone-sync devices list

# on .100: the sovereign folder is not in the config at all
syncthing cli config folders list

# on .100: nothing is holding the tree open
lsof +D ~/.skcapstone 2>/dev/null | head

# on .100: the control bus is still healthy and is NOT under ~/.skcapstone
~/.skenv/bin/skfleet control-bus audit
~/.skenv/bin/python -c "from skcapstone.fleet.paths import default_paths; print(default_paths().root)"
# must print a path under ~/.skfleet. If it prints ~/.skcapstone/fleet, STOP.
```

That last check is not paranoia theatre. If the nesting migration did not
actually land on `.100`, the fleet root is still inside the directory you are
about to remove, and the delete takes the control bus with it.

One thing to preserve first: `skmeter` writes
`~/.skcapstone/skmeter/<node>-state.json` and does **not** follow the fleet
root, by design (it is a recorded exemption in `test_root_relocation.py`
because it is a sibling of the fleet tree, not fleet state). If skmeter runs on
`.100`, copy that directory out before the delete or accept that the energy
counter rebaselines from zero:

```
cp -a ~/.skcapstone/skmeter ~/.skmeter-preserved 2>/dev/null || true
```

Then, and only then:

```
# on .100
rm -rf ~/.skcapstone
```

Verify:
```
# on .100
ls -d ~/.skcapstone            # expect: No such file or directory
~/.skenv/bin/skfleet control-bus audit   # expect: exit 0, unchanged

# on .158, .41 and norap2027 (the important one)
du -sh ~/.skcapstone
find ~/.skcapstone -maxdepth 2 -newermt '-15 minutes' -type d | head
# expect the size unchanged (19G on .158) and NO mass deletion.
# If anything under agents/ has just disappeared on a peer, the unshare did
# not take. Pause skcapstone-sync on every peer immediately and restore from
# ~/.skcapstone/backups before the deletion propagates further.
```

Revert: there is no clean revert of this step. `.100`'s copy is gone. The
sovereign tree still exists in full on `.158` and `.41`, so nothing unique was
lost, which is the reason this step is acceptable at all and the reason B4's
verification is about the *peers* rather than about `.100`.

### B5. Confirm the end state

```
# on .100
syncthing cli config folders list
# expect exactly one fleet-related folder: skfleet-control

~/.skenv/bin/skfleet node doctor
# expect the node Ready, heartbeating, with stateTier control-bus
```

`.100` now holds 368K of fleet state instead of 5.0G of sovereign state, runs
the same workloads, and can no longer receive agent memory even by accident,
because it is not a member of the folder that carries it.

---

## Rollback of the whole thing

If Part 1 is fine but you want to abandon Part 2, just leave `.100` in both
folders. Two transports for the same tree on the same node is wasteful and is
the overlap problem from [control-bus-nesting.md](control-bus-nesting.md), but
with the fleet root at `~/.skfleet` the two folders no longer share any files,
so it is merely redundant rather than harmful. Undo Part 1 at leisure by
deleting the `skfleet-control` folder on each node.

If Part 2 has completed and you want `.100` back on the sovereign folder,
re-add and re-share it and let Syncthing refill 5.0G from `.158`. That is a
long resync, not a data-loss event.

## What this runbook does not do

- It does not touch `sksync.skstack01.douno.it` on `.41`. Card `67e8c15f`.
- It does not make the `.stignore` rulesets managed or verified. They remain
  three hand-placed files that nothing checks. Card `20a1d4d3`.
- It does not remove `norap2027` from `skcapstone-sync`. That device is out of
  scope for the node-role model and appears here only as a peer whose config
  must be verified before the delete.
