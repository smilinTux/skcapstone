# Promotion drill, 2026-08-16

**Epic:** `3bbf39ea`. **Card:** `4c32df6f`.
**Subject:** [runbook-promotion.md](runbook-promotion.md), executed end to end
against a scratch fleet, both cases, plus fail-back.
**Harness:** `skfleet drill` (`src/skcapstone/fleet/drill.py`).
**Operator:** Lumina. **Production writes: zero** (evidence at the bottom).

The ADR ([adr-node-role-model.md](adr-node-role-model.md)) accepts `.158` as a
single control seat on the strength of a warm replica plus a drilled runbook.
Before today the runbook had never been executed. This is the record that
converts it, and it did not come back clean.

---

## Verdict

The control-plane promotion itself works. Every store-side step in the runbook
exists, runs as written, and reverts as written, in 5.7 seconds of wall clock
across 21 commands. That is the good news and it is the smaller half.

Nine gaps were found. Three of them mean that a promotion executed perfectly,
by the book, by a calm operator, leaves the fleet in a state the runbook says
is fine and that is not fine:

- **G1** the promoted seat cannot receive 26 of the fleet's own workloads,
  because `set-role` moves the role and the scheduler filters on a label.
- **G2** the runbook's only two-seat detector reports nothing for the
  overwhelmingly likely shape of a two-seat incident.
- **G3** the artifact the runbook tells you to hunt for, a `.sync-conflict-`
  file, silently overrides the real object when it lands in `objects/`.

G3 is the one to fix first. It turns the alarm into a second fault.

The identity gap **is** drillable, it was drilled, and it took 15.3 seconds.
The runbook was pointing at the wrong source (**G5**).

---

## What was executed

| phase | steps | executable in a scratch fleet? |
|---|---|---|
| guards | 11 refusal probes | yes, all |
| preconditions P1-P5 | 5 | P3, P4 yes. P1, P2, P5 are ssh/vault, reasoned only |
| Step 0 freeze + revert | 2 | yes |
| Case B B1-B5 | 5 | B5 yes. B1-B4 are `systemctl --user`, reasoned only |
| Phase 2, steps 2.1-2.7 | 7 | 2.1, 2.2, 2.6 yes. 2.3, 2.4, 2.5, 2.7 are systemd |
| fail-back F0-F6 | 7 | F0, F2, F3, F4, F6 yes. F1, F5 are systemd |
| two-seat experiments | 4 | yes, via a Syncthing emulator |
| key restoration | 1 | yes, against the real artifact |

The systemd steps are the honest limit of a scratch-fleet drill and are dealt
with under "What could not be drilled" below.

---

## Timings

Store-side, on `.158`, single run, cold Python start each time.

| step | time |
|---|---|
| `skfleet drill create` (3 profiles, 3 nodes, 1 service, seeded drift) | 2.75s |
| P3 conflict baseline (`find`) | 0.01s |
| P4 `skfleet nodes` | 0.33s |
| P4 `skfleet node doctor --all` | 0.31s |
| Step 0 `skfleet freeze` | 0.33s |
| Step 0 revert `skfleet unfreeze` | 0.46s |
| B5 confirm one writer | 0.01s |
| 2.1 re-verify replica (`cat _freeze.json`) | 0.00s |
| 2.2 `skfleet set-role <node> control` | 0.35s |
| 2.2 verify `skfleet describe` | 0.27s |
| **NEW** move the `control-plane` label (see G1) | 0.39s |
| 2.6 `skfleet unfreeze` | 0.46s |
| revert 2.2 `set-role <node> builder-standby` | 0.41s |
| F0 `skfleet freeze` | 0.37s |
| F3 demote `.41` | 0.43s |
| F4 `skfleet node doctor --all` | 0.25s |
| F6 confirm one seat | 0.36s |
| `skfleet drill teardown` | 0.41s |
| **total, 21 store-side steps** | **5.7s** |
| key restoration: extract 8 `private.asc` from the 296MB GFS artifact | 15.3s |

**Every revert listed was executed, not merely read.** All 21 steps returned 0.

The real promotion is therefore not rate-limited by the fleet store at all. It
is rate-limited by ssh, by systemd, and by the operator reading. That matters:
the runbook's "you have hours, not minutes" is correct, and there is no reason
to rush any part of the store-side work.

---

## The guards: 10 refusals fired, 1 did not

The harness's containment was negative-controlled before anything else, with
`SKFLEET_ROOT` deliberately exported to production for the whole set.

| probe | result |
|---|---|
| `--root ~/.skcapstone/fleet` | **refused** |
| `--root ~/.skcapstone` | **refused** |
| `--root <scratch>/../../../..<sovereign>/fleet/drilltest` | **refused** (resolved first) |
| `--root ~` | **refused** |
| `--root /` | **refused** |
| `--root` omitted, with `SKFLEET_ROOT=production` exported | **refused**, required option |
| `--root <symlink into the sovereign tree>/drilltest` | **refused** (resolved first) |
| `create` on a pre-existing unmarked directory | **refused**, contents intact |
| `teardown` on a pre-existing unmarked directory | **refused**, contents intact |
| `--root <a regular file>` | **refused** |
| `promote` while the control seat is still `Ready` | **refused** |

Every one of those is a PASS for the guard and they are recorded as such.

### G0. The containment is `$HOME`-relative

`drill.sovereign_home()` expands `~/.skcapstone` through `$HOME`, and
`resolve_drill_root` compares against that. Rewrite `$HOME` and the forbidden
prefix moves with it:

```
$ HOME=/tmp/fake-home python -c "from skcapstone.fleet import drill; \
    print(drill.resolve_drill_root('/home/cbrd21/.skcapstone/fleet/would-be-drill'))"
/home/cbrd21/.skcapstone/fleet/would-be-drill        # ACCEPTED
```

No write was performed to obtain that; `resolve_drill_root` only judges. But
`claim_root` would have gone on to `mkdir(parents=True)` and drop a marker
inside the live Syncthing folder, because the marker guard only fires on a
directory that already exists.

Reaching this needs `$HOME` rewritten **and** the production path spelled
absolutely. That is not an operator typo, it is `sudo`, a systemd unit with a
different `User=`, or a container with a remapped home. Low likelihood, but the
module's claim is that the refusal is *structural*, and a refusal that depends
on an environment variable is not.

**Suggested fix:** resolve the sovereign home through `pwd.getpwuid(os.getuid())`
rather than `$HOME`, or add a belt-and-braces refusal of any resolved path
containing a `.skcapstone` component. Not done here: it is a change to the
guard, and changing a guard inside the drill that is meant to be testing it is
the wrong order. Filed for a follow-up card.

---

## G1. The promotion moves the role. The scheduler filters on the label.

**Severity: high. This is the gap that makes a by-the-book promotion insufficient.**

`skfleet set-role node-41 control` writes `spec.role` and, by explicit design
(`node_controller.set_role` passes `labels=current.get("labels", {})`),
preserves labels untouched. The scheduler does not read `spec.role` at all.
`scheduler.feasible` filters on `view.labels`:

```python
for key, value in sorted(workload.node_selector.items()):
    if view.labels.get(key) != value:
        return f"selector mismatch ({key}={value})"
```

Production labels, read 2026-08-16:

| node | labels | `spec.role` |
|---|---|---|
| `node-noroc2027` | `always-on`, `control-plane`, `dev-primary`, `pi-harness`, `skcode-harness` | `control` |
| `node-41` | `heavy-build`, `pi-harness`, `skcode-harness` | `builder-standby` |

`.41` carries **neither** `always-on` nor `control-plane`. Production objects
that select on them:

- **17** on `{"always-on": "true"}`: `skgateway`, `skchat-daemon`, `skcomms`,
  `skmemory-daemon`, `skingest`, `skchat-coturn`, `skchat-piper-tts`,
  `skchat-nostr-relay`, `skchat-telegram-bridge-lumina`, `skchat-webui-lumina`,
  and 7 `skmem-*` / `skchat-backup-offbox` / `skcomms-housekeep` cronjobs.
- **9** on `{"control-plane": "true"}`: `skcapstone-daemon`, `skos-scheduler`,
  `capauth-keystore`, `skgateway-claude-wrapper`, `autopilot-daily`,
  `capauth-custody-doctor`, `skcapstone-backup-gfs`, `skgateway-parity-check`,
  `skos-morning-brief`.

**26 objects.** After a textbook promotion their only candidate node is the
dead one.

Reproduced in the scratch fleet with a service selecting `control-plane=true`,
mirroring the 9 above:

```
=== AFTER kill-control + textbook runbook promotion (set-role only) ===
node-drill-standby role=control (generation 6)
  node-drill-control   phase=Dead   role=control          -> not Ready (phase=Dead)
  node-drill-standby   phase=Ready  role=control          -> selector mismatch (control-plane=true)
  node-drill-worker    phase=Ready  role=worker-gpu       -> selector mismatch (control-plane=true)
  feasible nodes: NONE
```

The promoted seat is `Ready`, holds `role=control`, and the scheduler refuses
it. Nothing in the runbook's verify steps catches this, because `skfleet nodes`
prints `role=control` and looks exactly right.

### G1a. There is no `skfleet label` verb

`skfleet --help` lists no label command. The only way to move a label is
`skfleet apply -f FILE`, and that replaces the **whole spec**, because
`apply_cmd` does `spec = doc.get("spec", {})` and hands it to `write_spec`
verbatim. Drilled:

```
--- spec before ---
{"labels": {"drill":"true","role":"builder-standby"},
 "spec": {"address":"127.0.0.1  # node-drill-standby (drill)", "cordoned": true,
          "role":"control", "taints":[{"effect":"NoSchedule","key":"maint","value":"true"}]}}

$ skfleet apply -f label-fix.json      # doc states labels + {"role":"control"}
applied node/node-drill-standby (generation 9)

--- spec after ---
{"labels": {"control-plane":"true","drill":"true","role":"control"},
 "spec": {"role":"control"}}
```

The `address`, the `cordoned: true` and the entire `taints` array were dropped
silently, and the node went from cordoned to schedulable as a side effect of a
label edit. Exit code 0, no warning.

So the fix for G1 is itself a footgun, and the runbook must spell out the full
document rather than leaving an operator to write a minimal one under stress.

---

## G2. The two-seat detector is a collision detector, not a presence detector

**Severity: high. This is the drill's central negative result.**

The runbook's mechanism for noticing two operators is
`find ~/.skcapstone/fleet -name '*.sync-conflict-*'`, checked at P3, B5, 2.5
and F6. Two experiments, using a Syncthing emulator that models the real
semantics (a conflict is raised only when **both** sides changed since the last
agreed version, not merely when the two sides differ):

**Experiment 1, RACE.** Both seats write the same object inside one sync
interval:

```
  CONFLICT   objects/node/node-drill-worker.json (SEATB041 won)
  conflict files on .158: 1
  conflict files on .41 : 1
```

Detector fires. This is the case the runbook describes, and it is correct
about it.

**Experiment 2, INTERLEAVE.** The same two seats, same object, but Syncthing
converges between the writes. Five full rounds, ten operator writes from two
different machines:

```
  conflict files on .158: 0
  conflict files on .41 : 0
  final object: generation=12, taints=[], last writer=node-drill-standby
  10 writes from two different seats. The runbook's detector reports: 0
```

**Zero.** The store is internally consistent, every generation is sequential,
every writer block is well formed and signed, and there is no artifact anywhere
that a second seat existed.

Experiment 2 is the *likely* shape, not the exotic one. The fleet tree is 368K
([control-bus-folder.md](control-bus-folder.md)), Syncthing converges on it in
seconds, and `skoperator.timer` fires every 15 minutes. The probability that
two writes land inside the same sync interval is small. The probability that
they interleave cleanly is near one.

Worse, the runbook's own advice makes the detector less likely to fire:

> Wait one full timer cycle, 15 minutes, and re-run the conflict check.

A full timer cycle is precisely the interval that guarantees a sync happened in
between, which is precisely the condition under which no conflict file is
produced. The instruction is sound as a settling delay and actively misleading
as a detection strategy, because the runbook then asserts the converse:

> A new conflict file here means two operators.

True. But "no new conflict file" does **not** mean one operator, and the
runbook reads as though it does.

### A detector that does work

Every spec carries `writer.node`. A spec written by a node that does not hold
`spec.role: control` is a second seat, and it needs no collision to show up.
Run against the exact tree the conflict check had just cleared:

```
  nodes holding spec.role=control: ['node-drill-control']
  operator writes by node: {'node-drill-control': 6, 'node-drill-standby': 1}
  *** 1 spec(s) written by a node that is not the control seat:
      objects/node/node-drill-worker.json  <- node-drill-standby @ 2026-08-17T00:42:06Z
```

Caveat, stated because it matters: this is a **current-state** detector. Each
write overwrites the previous `writer` block, so it sees only the most recent
writer per object. If the foreign seat's writes all happened to be overwritten
afterwards by the real seat, it reports clean. It is strictly better than the
conflict check and it is still not proof.

### G2a. There is no write history at all

`store.write_spec` does not call `events.emit`. Nothing appends to
`status/<node>/events.jsonl` on a spec write. So after an incident there is no
way to answer "were there two seats, and for how long" from the store. The
current `writer` block is all there is, and it is one deep.

That is a design observation rather than a promotion bug, and it is the reason
G2's detector can only ever be best-effort. Worth a card.

---

## G3. A conflict file inside `objects/` silently overrides the real object

**Severity: high. Found while drilling the fail-back, and it is the worst one.**

`store.list_specs` does:

```python
for p in sorted(kind_dir.glob("*.json")):
```

and `node_controller.node_views` then does:

```python
admitted = {s["name"]: s for s in store.list_specs(paths, "node")}
```

keyed on the **`name` field inside the JSON**, not the filename. A Syncthing
conflict file is a byte copy of the losing object, so it carries the same
`name`. `node-drill-standby.json` sorts before
`node-drill-standby.sync-conflict-...json`, so the conflict file is last and
**wins the dict**.

Drilled, with a negative control in both directions:

```
=== ON DISK, the file Syncthing KEPT: ===
   spec.role = builder-standby  cordoned = True
=== skfleet describe (reads by path) ===
   spec.role = builder-standby  cordoned = True
=== skfleet nodes (reads via list_specs glob) ===
   node-drill-standby Ready role=control

=== NEGATIVE CONTROL: remove the conflict file, re-read ===
   node-drill-standby Ready CORDONED
=== put it back (under a different filename, so it is the glob, not the clock) ===
   node-drill-standby Ready role=control
```

Consequences:

1. **`skfleet nodes` and `skfleet describe` disagree**, on the same object, on
   the same box, in the same second. During an incident that is the difference
   between believing the promotion landed and knowing it did not.
2. **The version Syncthing discarded is the version the fleet acts on.**
   Everything downstream of `node_views` is affected, including
   `scheduler.feasible`, so the fleet schedules against the loser.
3. **A cordon can be silently lifted.** In the transcript above the live object
   is `cordoned: true` and the scheduler's view is `cordoned: false`, purely
   because a conflict file exists. Cordon is a safety operation.
4. It applies to every kind, not just nodes: `list_specs` is what backs
   `skfleet get cronjobs`, `skfleet services` and the controllers.

**Not currently live.** Production's two conflict files are
`status/node-noroc2027/heartbeat.sync-conflict-...json` and
`.../node.sync-conflict-...json`. Everything that reads the status tree does so
by exact path (`store.read_node_file`, `store.read_status`,
`merged()`), so those two are inert. The exposure is entirely future: the first
conflict that lands in `objects/` gets this behaviour, and a promotion is the
single most likely way to produce one.

**Suggested fix:** `list_specs` should skip any filename containing
`.sync-conflict-`, and `skfleet node doctor` should surface such files as an
error rather than leaving them to `find`. Filed for a follow-up card; changing
the store's read path is out of scope for a drill.

---

## G4. Case A's fail-back trap costs the promotion itself

The runbook is right that Case A never runs B1 and that F1 is its delayed
equivalent. It is right that `Persistent=true` fires the missed run at boot. It
tells you to expect a conflict file. It does not tell you what that conflict
file will have eaten.

Drilled: promote `.41` per Case A, then boot `.158` with its units still
enabled and let `skoperator` fire before Syncthing connects.

```
### F-TRAP: .158 powers back on. Persistent=true fires the missed run immediately.
###         .158 has NOT yet synced, so it acts on its pre-outage view.
  .158 wrote 2 specs before Syncthing connected.

### Syncthing connects:
  b->a new   objects/_freeze.json
  CONFLICT   objects/node/node-drill-standby.json (SEATA158 won)
  a->b       objects/node/node-drill-worker.json

### The state both boxes now share:
  node-drill-standby: gen=2 role=builder-standby cordoned=True writer=node-drill-control
```

`.158` never saw the promotion, so it wrote `node-drill-standby.json` at
generation **2**, the same generation `.41`'s promotion had written. Syncthing
resolved on mtime and `.158` won. **The promotion was silently reverted.** The
fleet now has one node claiming `role: control`, and it is the box that just
came back from an unexplained outage.

Generation is no help here: both writes are generation 2, both well formed.
The only surviving trace is the conflict file, and per G3 that conflict file is
now overriding the object in `skfleet nodes`, which will report `role=control`
for the demoted standby. The alarm and the corruption are the same artifact,
and the artifact lies.

---

## G5. The identity restore source named in the runbook does not exist. A working one does.

**The key-restoration step IS drillable. It was drilled. It took 15.3 seconds.**

The runbook says the eight missing agent keys have to come "from the sealed
vault or from the `agents/*/backups` tarballs". Checked on `.158`:

- `agents/*/backups` exists for exactly **one** agent, `lumina`, and `lumina`
  is one of the three agents `.41` already has. It covers **zero** of the eight.
- Even if it existed for all eight, `~/.skcapstone/.stignore` line 81 ignores
  `backups` and line 106 ignores `**/*.tar.gz`. It could never reach `.41` by
  Syncthing under any circumstances.
- `skvault` is locked, and whether it holds those eight keys has never been
  verified. Discovering it does not, during an incident, is not a position to
  be in.

The source that actually works was found by following
`objects/cronjob/skcapstone-backup-gfs.json` to `scripts/backup-gfs.sh`, which
has an opt-in `OFFSITE_DEST` rsync push. It is configured, and the log says so:

```
[2026-08-16T06:46:22Z] Daily backup: .../daily/skcapstone-state-20260816-024501.tar.gz (296M)
[2026-08-16T06:50:46Z] Off-site push OK -> 192.168.0.41:/home/cbrd21/skcapstone-offsite/158
```

Verified on `.41` itself, read-only over the tailnet:

```
$ ssh cbrd21@100.86.156.5 'ls ~/skcapstone-offsite/158/gfs/daily/ | tail -2'
skcapstone-state-20260816-024501.tar.gz
skcapstone-state-20260816-024501.tar.gz.sha256
$ ... tar tzf ... | grep -c 'capauth/identity/private.asc'
11
```

**`.41` is already holding, today, a same-day artifact containing all 11 agent
private keys.** It also contains `capauth/service/oidc_signing_key.pem` and the
complete `skcomms/cot-pki` set (CA, server, 5 device keypairs, 16 `.pem`/`.key`
files). That is every one of the three classes
[replica-verification.md](replica-verification.md) recorded as missing.

This works precisely because it is **not** Syncthing. It is an rsync push to a
path outside `~/.skcapstone`, so the `.stignore` rules that (correctly) keep
loose private keys off the replica do not apply to the tarball.

The restoration drilled against the identical local copy of that artifact:

```
extract took 15.3s
  architect   7435 bytes  keyid=A25BB1BC978C28F5
  artisan     7426 bytes  keyid=B5339D80DB84A1B2
  ava         7491 bytes  keyid=21B14F6B68D703C8
  coder       7422 bytes  keyid=D2AF37ED81F2114C
  herald      7426 bytes  keyid=2252018B4741AA21
  scholar     7431 bytes  keyid=8FC45C6D16DA8D10
  sentinel    7431 bytes  keyid=7508775E5CFBE52C
  steward     7431 bytes  keyid=D8634AB3E7B55236
```

All eight parse as real secret keys under `gpg --show-keys`. No vault, no
`.158`, no network beyond `.41`'s own disk.

**So the honest statement of the SPOF is narrower again than the runbook's.**
The mitigation covers state via Syncthing and identity via the nightly off-site
rsync, with a worst case of one day of key staleness (which for long-lived PGP
keys is no staleness at all). What the runbook had was a pointer at a source
that does not exist.

### G6. The backup that insures against `.158` can only run on `.158`

`objects/cronjob/skcapstone-backup-gfs.json` carries
`nodeSelector: {"control-plane": "true"}`, a label only `node-noroc2027` holds.
Its own `note` field says "must ship off-box (circular-DR theme 4)", and it
does, which is what saves G5. But the job itself is schedulable on exactly one
node: the one whose death it exists to survive.

Combined with G1, after a promotion it is schedulable on **no** node, so the
off-site rotation that just rescued the identity restore quietly stops
advancing while `.41` holds the seat. Nobody would notice until the next
incident.

---

## G7. The runbook's own "Drilling this" section is broken and obsolete

Run exactly as written:

```
$ export SKFLEET_ROOT=/tmp/drill-fleet
$ cp -r ~/.skcapstone/fleet/* /tmp/drill-fleet/
cp: target '/tmp/drill-fleet/': No such file or directory
```

No `mkdir`. It fails on its second line.

More importantly it is the wrong shape. It tells the operator to copy
production into a drill root, which is exactly the pattern `skfleet drill`
exists to make unnecessary and which its ownership marker exists to make
impossible. And it does not mention `skfleet drill` at all.

---

## G8. `skfleet reconcile` is silent about the freeze

The runbook's claims about freeze were negative-controlled in both directions
and are **correct as written**:

```
frozen: true,  placement deleted, then reconcile:
  placed=0 kept=0 failovers=0 alerted=0 skipped=0
  placement file recreated? no (freeze gated it)
unfrozen, same:
  placed=1 kept=0 failovers=0 alerted=0 skipped=0
  placement file recreated? YES
```

Freeze gates actuation. It does not gate authorship: `set-role`, `taint`,
`cordon`, `untaint` and `uncordon` all succeeded while frozen, exactly as the
runbook says.

The small gap is that `reconcile` reports the frozen no-op as
`placed=0 ... skipped=0` with exit 0 and no mention of the freeze. An operator
who runs it mid-incident cannot tell "frozen, refused" from "nothing to do".
Cosmetic, but it is a lie of omission in the one place someone is stressed.

---

## G9. The harness drills a promotion the runbook does not prescribe

`drill.DrillFleet.promote()` executes three steps: cordon the lost seat, taint
it `control-seat=lost:NoSchedule`, then `set-role` the replica. The runbook's
main sequence contains only the third, and its command index explicitly argues
against the first two:

> `skfleet taint` and `skfleet untaint` are not used in the main sequence, and
> that is on purpose [...] knowing they will not help is worth more than the
> instinct.

So the harness rehearses a procedure that the document it is rehearsing tells
you not to follow. Both are defensible in isolation and they should not
disagree. Either the runbook adopts cordon and taint as belt-and-braces on a
seat that might come back, or the harness drops them. Flagged, not resolved:
picking one is a judgement call for Chef, and the reverts on both sides work
either way (`revert_promotion()` was executed and round-tripped cleanly).

---

## G10. The drill's own production-safety test was flaky, and would have read as a regression

Found during verification, not during the drill, and fixed here.

`tests/fleet/test_drill.py` proves the harness never writes production by
snapshotting the **live** `~/.skcapstone/fleet` before and after, then asserting
no differences except same-size `heartbeat.json` changes, which it correctly
attributes to other nodes' `sknoded`.

The carve-out was too narrow. `sknoded` also rewrites `node.json` on the same
cycle, and `skoperator.timer` refreshes `objects/operatorapp/*.json` and service
specs every 15 minutes. Any of those landing inside a 150-second full-suite run
fails the assertion. It did:

```
FAILED tests/fleet/test_drill.py::test_ambient_skfleet_root_is_never_used_as_the_target
============ 1 failed, 817 passed in 150.23s ============
$ pytest tests/fleet/test_drill.py -q
============ 44 passed in 4.64s ============
```

Same commit, docs-only change, green in isolation. That is a red gate that
looks exactly like "the drill harness started writing production" and is not,
which is the specific way a gate stops being a signal.

**Fixed** by forgiving any same-size, mtime-only change rather than enumerating
filenames. That is strictly stronger where it counts: if the harness ever
touched production it would **create** paths (a tree plus its `.skfleet-drill`
marker) or **remove** them (`teardown` is a recursive delete), and any spec it
wrote would change that file's size, because `write_spec` bumps `generation`
and rewrites the document. A same-size mtime bump is the one shape the drill
cannot produce and the one shape other nodes' daemons produce constantly.

Negative-controlled before committing:

```
mtime-only change (daemon):    forgiven  <- the flake, now silent
size change (a spec write):    {'b.json': ((200, 1), (201, 2))}
new path (a drill tree):       {'.skfleet-drill': (None, (50, 1))}
removed path (a teardown):     {'c.json': ((300, 1), None)}
```

Worth noting the general shape: this is a test that reads live production state
on every run. It is the only one in the fleet suite that does, and it is
load-bearing enough to keep. But it will always be sensitive to fleet activity,
and anyone touching it should negative-control it rather than just widen it
until it passes.

---

## What could not be drilled, and what it would take

**The systemd half.** B1-B4, 2.4, 2.5, 2.7, F1 and F5 are all
`systemctl --user enable/disable`. `SKFLEET_ROOT` has no equivalent for the
systemd user manager, so these cannot be redirected into a scratch tree, and
running them for real on `.158` would disable the live control seat.

Drilling them needs one of:

- a **scratch user account** on `.158` or `.41` with its own
  `~/.config/systemd/user/`, given copies of the 21 unit files with their
  `ExecStart` pointed at `/bin/true`. Cheap, and it would genuinely exercise
  `daemon-reload`, `enable --now`, `is-enabled`, and the `Persistent=true`
  boot-fire behaviour that G4 turns on.
- or a **throwaway VM/container** with a systemd user session, which is the
  only way to drill the reboot in F1 honestly.

Recommendation: the scratch-user route, as a follow-up card. It is a few hours
and it covers the majority of the runbook's remaining unexecuted steps. Note it
would *not* cover F1's core risk, which is a real boot.

**P1, P2, P5.** `ssh` reachability, replica currency and `skvault unlock` are
about the live fleet by definition. P2's spirit was drilled in a different form
(the two-seat and fail-back experiments both turn on replica currency), and P5
is partly answered by G5: the vault is no longer the only key source.

**A real Syncthing.** The two-seat and fail-back results come from a 40-line
emulator that implements last-synced-baseline conflict semantics. It models
what Syncthing documents. It does not model version vectors, `.stversions`,
partial transfers, or clock skew between the two boxes. The qualitative
findings (G2's interleave producing no artifact, G3's glob override, G4's
same-generation overwrite) do not depend on those details, but a second drill
against two real Syncthing instances would be worth doing before anyone treats
G2 as settled.

---

## What worked exactly as written

Recorded because a drill that only lists failures is not a report.

- `skfleet freeze` / `unfreeze`, and their reverts, both directions, verified
  by the file contents each time.
- Freeze semantics: gates actuation, not authorship. Negative-controlled both
  ways (G8). The runbook's careful two-halves explanation is accurate.
- `skfleet set-role` round-trips `taints`, `cordoned` and `address` untouched
  and bumps `generation` by exactly one, as Step 2.2 claims. Verified across
  four consecutive role changes.
- The harness refuses to promote while the seat is `Ready`, with an error that
  explains why. That is the reflex the drill should teach and it teaches it.
- P3's expectation is exact: two pre-existing conflict files, both under
  `status/node-noroc2027/`, `heartbeat` and `node`. The runbook's instruction
  to baseline them before promoting is correct and, per G3, more important than
  it knew.
- `skfleet node doctor --all` produced a real finding on the seeded drift
  (`unexpected_units rogue-drill.service`) rather than a clean report, so the
  drill proves the doctor was actually consulted.
- Every command in the runbook's command index that touches the store exists
  and behaves as the table says.

## What was tried that found nothing

- **Status-tree conflicts.** Checked whether production's two existing conflict
  files could be masking heartbeats the way G3 masks specs. They cannot:
  `read_node_file`, `read_status` and `merged()` all address by exact path.
  Inert. This is where G3 was found, by asking the same question one directory
  up and getting a different answer.
- **`.stignore` and conflict files.** Checked whether fleet conflict files are
  themselves ignored, which would make the runbook's `find` useless on the
  remote box. They are not: line 100 drops
  `coordination/itil/**/*.sync-conflict-*` only, and line 69 drops `*.db`
  conflicts. Fleet JSON conflicts propagate normally, so the runbook is right
  to say run `find` on both boxes.
- **Freeze plane-file ownership.** Tried to find a path by which the autonomous
  seat could unfreeze itself. `store.set_frozen` and `write_plane_file` both
  refuse `agent_seat=True` before anything else. No way in from the CLI.
- **Name validation.** Tried `..` and `/` in kind and object names through
  `apply`. `paths.valid_name` rejects both.

---

## Production untouched: evidence

Snapshot of `find . -printf '%p\t%s\t%T@\n'` under `~/.skcapstone/fleet`, taken
at 2026-08-16T20:35:52-04:00 (before) and after the entire drill. 106 paths
both times.

- **Files added: none. Files removed: none.**
- **Every changed path has an identical byte size.** No path changed size.
- Changed paths fall into three groups:
  - `status/node-41/{heartbeat,node}.json`,
    `status/node-noroc2027/{heartbeat,node}.json`,
    `status/node-ollama/{heartbeat,node}.json`. Independent `sknoded`
    heartbeats from three nodes, identical sizes. Expected, and named as
    expected in the card.
  - `objects/operatorapp/*.json` (7 files) and `objects/service/skgateway.json`.
    Not covered by the heartbeat carve-out, so checked by content rather than
    assumed: all eight carry `writer.node=node-noroc2027` and
    `updatedAt=2026-08-17T00:41:5xZ`, and `systemctl --user list-timers` shows
    `skoperator.timer` last fired at 20:41:50 EDT, which is 00:41:50 UTC. These
    are the scheduled operator seat's own 15-minute pass. Generations are in
    the 1600s, a long-running counter this drill did not touch.
  - `atlas/brief/{brief.md,index.html}`. The brief generator, identical sizes.

Every `skfleet` invocation in this drill carried an explicit `SKFLEET_ROOT`
into a scratch tree under the session scratchpad. The harness was additionally
run against production paths eleven times on purpose, and refused every time.

Reads of production (`~/.skcapstone/fleet/objects`, `.stignore`, the GFS
artifacts, `.41` over ssh) were all read-only. No node had `spec.actuate`
enabled and none was changed.

---

## Follow-up cards this drill should generate

| # | gap | suggested action |
|---|---|---|
| 1 | G3 | `store.list_specs` skips `*.sync-conflict-*`; `node doctor` reports them as an error |
| 2 | G1 | give `.41` the `control-plane` and `always-on` labels now, or add a `skfleet label` verb, or make `set-role` move role-implied labels |
| 3 | G2 | ship the `writer.node` audit as `skfleet control-bus audit` output or a `node doctor` check |
| 4 | G6 | remove the `control-plane` selector from `skcapstone-backup-gfs`, or add the label to `.41` |
| 5 | G0 | resolve the sovereign home through the passwd database, not `$HOME` |
| 6 | systemd | scratch-user systemd drill for B1-B4, 2.4, 2.5, 2.7, F1, F5 |
| 7 | G2a | emit an event on `store.write_spec` so there is a write history |
| 8 | G9 | reconcile the harness's cordon+taint with the runbook's position on taints |
