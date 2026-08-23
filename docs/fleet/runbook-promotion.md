# Runbook: promote `.41` to control on loss of `.158`

**Epic:** `3bbf39ea`. **Cards:** `591d2b1a` (the runbook and drill),
`0afa9ffb` (a documented revert on every step), `4c32df6f` (the drill run).
**Precondition sibling:** `6bcf1e4c` verifies `.41` actually holds a current
replica.
**Companions:** [control-unit-set.md](control-unit-set.md) (what `.41` gains,
unit by unit), [adr-node-role-model.md](adr-node-role-model.md) (why the SPOF
is accepted and this is its mitigation),
[promotion-drill-2026-08-16.md](promotion-drill-2026-08-16.md) (the drill
record: what was executed, what broke, and the evidence for every claim marked
DRILLED below).

**Status: DRILLED 2026-08-16** against a scratch fleet, both cases plus
fail-back. Everything below marked **[drilled]** was executed, including its
revert. Everything marked **[not drilled]** is systemd work that has no
scratch-tree equivalent and is still reasoning, not capability. The drill
changed this document in five places; they are marked **[added by the drill]**.

---

## Read this part first

You are here because `.158` is not answering. Three things are true and none
of them are obvious under stress:

**1. Nothing is dying while you read.** Losing the control seat does not stop
the fleet. Every service that was already converged keeps running. `sknoded`
on every node keeps reporting. Syncthing keeps replicating. What you have lost
is the ability to write a **new** spec, which means the fleet is frozen in its
current shape, not falling over. **You have hours, not minutes.** Slow down.

**2. The dangerous mistake is not being too slow, it is being too fast.** The
single-writer invariant is enforced by role, not by machine: `store.write_spec`
checks `writer.role != "operator"` and nothing else, and `skfleet`'s
`_operator()` claims that role on whatever box it is invoked from. `.41`
already has the `skfleet` CLI and already has a full copy of the fleet tree.
**`.41` can write specs today, without being promoted at all.** If `.158` is
alive but sick and you promote `.41` anyway, you get two operators, and
Syncthing will not error, it will pick a winner by timestamp and leave a
`.sync-conflict-` file that nobody reads. A spec you wrote will silently not
be the spec that is live.

**3. Promotion is reversible if you do it in the documented order and
irreversible if you improvise.** Every step below has a revert line. Use them.

If you only do one thing before touching anything: run **Step 0**, the freeze.
It is cheap, it is instantly reversible, and it stops the automation from
making decisions while you are making yours.

---

## The replica does NOT carry the agent signing keys. Read this before anything else.

Card `6bcf1e4c` verified the replica and found the one thing a promotion cannot
recover from on its own.

`.41` holds a genuinely current replica: all 18 sovereign source-of-truth classes
hash byte-for-byte identically, so a promoted `.41` inherits every memory, card,
seed, soul and coordination record. What it does NOT inherit is the ability to
sign as most of the agents that own them.

Measured 2026-08-16:

| node | private key files under `~/.skcapstone` | agents with `private.asc` |
|---|---|---|
| `.158` | 28 | architect, artisan, ava, coder, herald, jarvis, lumina, opus, scholar, sentinel, steward |
| `.41` | 4 | jarvis, lumina, opus |

**Eight agents' signing keys exist only on `.158`**: architect, artisan, ava, coder,
herald, scholar, sentinel, steward. Also absent from the replica:
`capauth/service/oidc_signing_key.pem` and the whole `skcomms/cot-pki` set (CA,
server, and five device keys). Roughly 25 files, 88KB.

This is not a bug and not a Syncthing failure. It is the `.stignore` rules
(`*.key`, `*.pem`, `**/private.*`) doing exactly their job: private key material
must never leave the node that owns it. The same three lines that keep 11 agent
keys off the GPU worker also keep 8 of them off the standby.

So the honest statement of the accepted SPOF is narrower than the ADR implies.
The mitigation covers STATE, not IDENTITY. A promoted `.41` is a working control
seat that cannot sign as eight of its agents until those keys are restored from
backup or the agents are re-keyed.

Two consequences for this runbook:

1. **Restoring the eight keys is a promotion step, not an afterthought.** The
   source is the nightly off-site GFS tarball, which is already sitting on `.41`.
   See the step below.
2. **The operator key is not part of this problem.** `capauth/identity/` on `.158`
   holds `public.asc` only, so operator custody was never a Syncthing question and
   is not fixed or broken by a promotion.

If you are promoting under time pressure and the eight agents are not needed
immediately, promote first and restore keys after. Just do not believe the seat is
whole until they are back.

### Step K. Restore the keys, on `.41`, from `.41`'s own disk **[drilled, 15.3s]**

**[added by the drill]** An earlier version of this runbook said the keys had to
come "from the sealed vault or from the `agents/*/backups` tarballs". Both were
wrong, and the drill found the source that works.

- `agents/*/backups` exists for exactly one agent, `lumina`, which `.41` already
  has. It covers **none** of the eight.
- It could never have worked anyway: `~/.skcapstone/.stignore` ignores `backups`
  (line 81) and `**/*.tar.gz` (line 106), so no tarball has ever replicated.
- `skvault` may or may not hold them. Nobody has checked, and an incident is the
  wrong time to find out.

What actually works: `scripts/backup-gfs.sh` has an `OFFSITE_DEST` rsync push,
it is configured, and it runs nightly. **`.41` already holds a same-day tarball
containing every missing key.** rsync is not Syncthing, and the destination is
outside `~/.skcapstone`, so the `.stignore` rules do not apply to it.

```
# on .41. no vault, no .158, no network beyond this box.
ls -t ~/skcapstone-offsite/158/gfs/daily/*.tar.gz | head -1
```

Confirm it is current and complete before you rely on it:

```
L=$(ls -t ~/skcapstone-offsite/158/gfs/daily/*.tar.gz | head -1)
sha256sum -c "$L.sha256"
tar tzf "$L" | grep -c 'capauth/identity/private.asc'    # expect 11
tar tzf "$L" | grep -E 'oidc_signing_key.pem|cot-pki/'   # expect the full set
```

Extract to a staging directory first, never straight over `~/.skcapstone`:

```
mkdir -p /tmp/keyrestore
tar xzf "$L" -C /tmp/keyrestore \
  $(for a in architect artisan ava coder herald scholar sentinel steward; do \
      echo ".skcapstone/agents/$a/capauth/identity/private.asc"; done)

for a in architect artisan ava coder herald scholar sentinel steward; do
  gpg --show-keys /tmp/keyrestore/.skcapstone/agents/$a/capauth/identity/private.asc
done
```

Only once all eight parse, copy them into place with mode 600 and restore
`capauth/service/oidc_signing_key.pem` and `skcomms/cot-pki/` the same way.

**Revert:** delete what you copied in. These are restores of files that were
absent, so the revert is `rm` of the eight paths plus the two other classes, and
`.41` is back to its pre-promotion identity state. Nothing was overwritten.

**Verify:** `capauth doctor` per agent, and note that the eight keys are as of
last night's 02:45 run. For long-lived PGP keys that is not staleness.

**Timing, measured:** 15.3s to extract the eight from a 296MB artifact.

**One catch, and it is the reason this needs watching.** The cronjob that
produces this artifact, `skcapstone-backup-gfs`, carries
`nodeSelector: {"control-plane": "true"}`, a label only `.158` holds. See the
label warning in Step 2.2: after a promotion that job is schedulable on **no**
node, so the rotation that just saved you stops advancing. Fix the label, or
run the backup by hand while `.41` holds the seat.

## Preconditions

Run all five. They are read-only. Write the answers down, on paper if you have
to, because you will want them again during fail-back.

### P1. Is `.41` reachable?

```
ssh cbrd21@100.86.156.5 hostname
```

Expect `cbrd21-laptop12thgenintelcore`. Note the address: `.41` is
**Tailscale-only**. The old `192.168.0.41` is dead and will hang rather than
refuse, which reads like a dead box when it is a dead route.

**If it fails:** you have lost both the control seat and the promotion target.
Do not proceed. This is a different incident. Bring `.41` up first, or bring
`.158` up, whichever is closer to possible.

### P2. Is `.41` holding a CURRENT replica? (card `6bcf1e4c`)

This is the precondition the whole runbook rests on. Promoting a node holding
a stale fleet tree means writing new specs on top of an old world.

```
ssh cbrd21@100.86.156.5 'ls -la ~/.skcapstone/fleet/objects/ ~/.skcapstone/fleet/objects/node/'
ssh cbrd21@100.86.156.5 'systemctl --user is-active syncthing.service'
ssh cbrd21@100.86.156.5 'syncthing cli show connections'
```

Three questions, in order of how much they tell you:

- **Does the tree exist and is it populated?** You want `objects/node/`,
  `objects/profile/`, `objects/service/`, `objects/cronjob/`,
  `objects/operatorapp/`, plus `_freeze.json` and `_protected.json`.
- **Is Syncthing running there?** If it is stopped, the replica is as old as
  the moment it stopped, and that could be days.
- **When did it last connect?** `syncthing cli show connections` gives
  per-device `connected` and `at`. A device that has not connected since
  before the incident is a replica frozen at that time.

If `.158` is still readable at all, compare generations directly. This is the
strongest check available:

```
# on .158
cat ~/.skcapstone/fleet/objects/node/node-41.json | grep -E '"generation"|"updatedAt"'
# on .41
ssh cbrd21@100.86.156.5 'grep -E "\"generation\"|\"updatedAt\"" ~/.skcapstone/fleet/objects/node/node-41.json'
```

Equal `generation` on both sides means the replica is current for that object.
Check two or three objects, not one.

**If the replica is stale:** do NOT promote yet. Start Syncthing on `.41`, or
wait for it to converge, and re-check. If `.158` is gone and the replica is
stale, you have a **data-loss decision, not a promotion decision**: read the
"Data-loss window" section below before you write anything, because the first
spec write on `.41` bumps `generation` and makes the divergence permanent.

**If `.41` has no fleet tree at all:** stop. Restore it from the most recent
`skcapstone-backup-gfs` artifact before proceeding. A promotion onto an empty
store is not a promotion, it is a new fleet.

### P3. Are the sync-conflict files already there?

```
find ~/.skcapstone/fleet -name '*.sync-conflict-*'
ssh cbrd21@100.86.156.5 "find ~/.skcapstone/fleet -name '*.sync-conflict-*'"
```

**Expect hits. As of 2026-08-16 there are two**, both under
`status/node-noroc2027/`, and both node objects report the condition
`SyncConflict / DoctorProbe` as `True`. That is pre-existing and it is not
your incident. Record the list **before** you promote so that after promotion
you can tell your conflicts from the old ones. A conflict file that appears
during promotion is the split-brain alarm, and you cannot hear an alarm you
cannot distinguish from the background.

### P4. What does the fleet think its own state is?

```
skfleet nodes
skfleet describe node node-41
skfleet describe node node-noroc2027
skfleet node doctor --all
```

Run these on whichever box answers. Record `skfleet nodes` output verbatim:
role, labels, capacity, and heartbeat age for every node. The heartbeat age is
your evidence for the Case A / Case B decision below.

**Known gap, do not be surprised by it:** `skfleet node doctor --all` prints
`skipped node-41: has published no inventory yet`, so there is no automated
drift check for `.41`. And `skfleet node doctor node-41` does **not** fix that:
with an explicit name it collects the **local** inventory and grades it
against `node-41`'s profile, which on `.158` means grading `.158`'s units as
if they were `.41`'s. It produces a confident, wrong answer. Use `--all`, read
the skip, and fall back to `systemctl --user list-unit-files --state=enabled`
over ssh.

### P5. Do you have what the missing units need?

Per [control-unit-set.md](control-unit-set.md), `.41` is missing **21 unit
files**, including `skoperator.timer` and `capauth-authz.service`, the two
that actually constitute the control seat. Several of the others need secrets
that are not on `.41`: Telegram bot tokens, TURN credentials, nostr relay
keys, TAK certificates.

```
skvault unlock --word rubikscube    # you will need this; confirm it works BEFORE you need it
```

**If skvault will not unlock:** you can still complete the control-plane
promotion (Phase 2 below), which needs no secrets. You cannot complete the
service restoration (Phase 3). Do Phase 2, then stop and solve skvault.

---

## Which case are you in?

This is the only branch in the runbook, and it changes the **order**, not the
steps. Getting it wrong in the safe direction costs you time. Getting it wrong
in the unsafe direction costs you the fleet store.

| | **Case A: `.158` is GONE** | **Case B: `.158` is ALIVE BUT DEGRADED** |
|---|---|---|
| looks like | no ping, no ssh, no heartbeat, and you know why (PSU, disk, theft, fire) | ssh is slow or flaky, some services are up, heartbeat is intermittent, disk full, OOM |
| the risk | data loss from an un-replicated write | **two operators writing the same store** |
| the order | promote `.41`, then worry about `.158` | **demote `.158` first**, then promote `.41` |

**When you are not sure, you are in Case B.** A box that might come back is a
box that will come back, at the worst moment, running `skoperator.timer` on a
15-minute cycle against a store that has moved on without it. Treat ambiguity
as Case B. The cost is one extra step.

The specific tell: a `.158` heartbeat under 2 minutes old in `skfleet nodes`
means `sknoded` is running there, which means the box is alive enough to run
timers. That is Case B no matter how bad ssh feels.

---

## Step 0. Freeze (both cases, always first)

```
skfleet freeze --reason "158 loss, promoting 41 per runbook-promotion.md, <your name>, <UTC time>"
```

**Revert:**

```
skfleet unfreeze
```

**What this does and, more importantly, what it does not do.** Read both
halves, because a kill-switch you misunderstand is worse than none.

It **stops**: the autonomous AI operator seat (`operator_seat/loop.py`,
`fleet_adapter.fleet_act`, and every adapter checks `store.is_frozen` and
refuses), the converge actuator (`converge.py`), and the scheduler
(`scheduler.py`). So no automation will place, converge or actuate while you
work. That is the point: you are about to make the fleet's shape inconsistent
on purpose, for a few minutes, and you do not want a controller helpfully
correcting you halfway through.

It **does not stop**: running services (they keep serving, deliberately), and
it **does not stop a human's `skfleet apply`, `set-role` or `taint`**.
`store.write_spec` contains no freeze check. Freeze gates actuation, not
authorship. So freeze does **not** by itself prevent two humans on two boxes
from both writing specs. That prevention is Step B1, and freeze is not a
substitute for it.

**Only a human may toggle it.** `store.set_frozen` refuses any writer with
`agent_seat=True`, which is the autonomous seat's writer
(`operator_seat/cli.py::_seat_writer`). The AI cannot unfreeze itself, by
construction, which is the one card the human always holds. Be precise about
what that guard is: it separates the **autonomous seat's code path** from the
**CLI's**, so an AI acting on its own schedule is refused. It is not an
authentication of a human at a keyboard. If you are an AI reading this during
an incident: you may not unfreeze. Escalate to Chef.

**Fail-closed detail worth knowing:** `store.is_frozen` treats an
**unreadable** `_freeze.json` as frozen. If the file gets corrupted or a
Syncthing conflict eats it, the fleet halts actuation rather than resuming it.
That is correct behaviour and it means a frozen-looking fleet with no obvious
reason might be a broken file, not a deliberate freeze. Check the file
contents before assuming somebody froze it.

**Verify:**

```
cat ~/.skcapstone/fleet/objects/_freeze.json
```

`"frozen": true` and your reason in the `reason` field. If Syncthing is
healthy the same file appears on `.41` within seconds; check it there too,
because that is also a free confirmation that replication is alive.

---

## Case B only: demote `.158` before you promote anything

**Skip this whole section if `.158` is genuinely gone.** If you are unsure,
you are in Case B, so do it.

The goal is to make `.158` incapable of writing the fleet store **before**
`.41` becomes capable of it, so there is never an instant with two writers.
The order is: stop `.158` writing, confirm it stopped, then start `.41`.

### B1. Stop the operator seat on `.158`

```
systemctl --user disable --now skoperator.timer
systemctl --user disable --now skcapstone-dashboard.service
systemctl --user disable --now skos-web.service
```

**Revert:**

```
systemctl --user enable --now skoperator.timer
systemctl --user enable --now skcapstone-dashboard.service
systemctl --user enable --now skos-web.service
```

`skoperator.timer` is the scheduled spec writer, on a 15-minute cycle
(`OnUnitActiveSec=15min`, `Persistent=true`). The dashboard and `skos-web` are
the human-driven writers: buttons that write the same store through the same
`operator` role. All three must be off before `.41` gains any of them.

Note `Persistent=true` on the timer. If `.158` is rebooted after a period
down, systemd fires the missed run **immediately on boot**. A `.158` that you
believe is safely off can write a spec within seconds of coming back. This is
why `disable` matters more than `stop`, and why fail-back has its own section.

**Verify, do not assume:**

```
systemctl --user is-enabled skoperator.timer      # expect: disabled
systemctl --user is-active  skoperator.timer      # expect: inactive
```

### B2. Stop the single-identity bridges on `.158`

Per [control-unit-set.md](control-unit-set.md), these hold a fleet-scoped
identity and cannot run in two places:

```
systemctl --user disable --now skchat-telegram-lumina.service
systemctl --user disable --now skchat-telegram-opus.service
systemctl --user disable --now skchat-nostr-relay.service
systemctl --user disable --now skcot.service
systemctl --user disable --now skcode-hostd.service
systemctl --user disable --now skcomms-api.service
systemctl --user disable --now skcomms-signaling-broker.service
systemctl --user disable --now skcomm-daemon.service
```

**Revert:** the same list with `enable --now`.

Defer this if `.158` is degraded but the bridges are working and you are not
yet ready to stand them up on `.41`. A working bridge on a sick box beats no
bridge anywhere. **But you may not enable the `.41` copy until the `.158` copy
is off.** Doing them one pair at a time, disable-then-enable, is fine and is
often the calmer path.

### B3. Stop the shared-destination timers on `.158`

```
systemctl --user disable --now capauth-backup.timer
systemctl --user disable --now skchat-backup.timer
systemctl --user disable --now skcomm-queue-drain.timer
systemctl --user disable --now skingest-maintain.timer
```

**Revert:** the same list with `enable --now`.

These write off-box. Two racing on one destination produces a half-written
archive that both runs believe they completed, and a queue drain that
double-delivers or double-claims. Nothing will complain if you forget these,
which is exactly why they are listed.

### B4. Leave these RUNNING on `.158`

Do not touch them. Turning them off is the mistake, not the fix:

- **`syncthing.service`** stays up. It is how `.41` learns anything. Stopping
  it is how you create a data-loss window rather than close one.
- **`sknoded.service`** stays up. `store.write_status` enforces
  `writer.node == node`, so `.158`'s noded can only write `.158`'s own status
  subtree. It cannot collide with `.41`'s. Its heartbeat is also how you will
  know when `.158` recovers.
- **`capauth-authz.service`** stays up. It is a stateless PDP on
  `127.0.0.1:8420`; every host that gates anything needs its own copy.
- **`skgateway.service`**: see Step 2.3. Do not disable it here.

### B5. Confirm the store has one writer

```
find ~/.skcapstone/fleet -name '*.sync-conflict-*'
```

Compare against your P3 baseline. **No new conflict files** means one writer.
New conflict files at this point mean something on `.158` is still writing and
you have not found it. Find it before continuing. Do not proceed on hope.

---

## Phase 2: promote `.41` (both cases)

Everything from here runs **on `.41`** unless it says otherwise.

```
ssh cbrd21@100.86.156.5
```

### Step 2.1. Re-verify the replica on `.41` itself

Do this again, on `.41`, even though you did P2. If Case B took a while, the
picture has moved.

```
ls ~/.skcapstone/fleet/objects/node/
cat ~/.skcapstone/fleet/objects/_freeze.json     # expect frozen: true, your reason
find ~/.skcapstone/fleet -name '*.sync-conflict-*'
```

Seeing **your own freeze reason** on `.41` is the single best evidence you
will get that replication is live in the direction you need. It is a message
you wrote, on `.158`, arriving here. If it is present, the replica is current
to within seconds.

**Revert:** none. Read-only.

**If your freeze reason is not there:** replication is broken or lagging.
Stop. Fix Syncthing before you write anything, or accept the data-loss
decision consciously (see below).

### Step 2.2. Bind the role **[drilled, 0.35s]**

```
skfleet set-role node-41 control
```

**Revert:**

```
skfleet set-role node-41 builder-standby
```

This is the actual promotion as far as the fleet object model is concerned:
it writes `spec.role` on the node object, bumping its generation, going out
over Syncthing to every node. `set-role` overlays one field and rewrites
through `store.write_spec`, so `taints`, `cordoned`, `address` and `identity`
all round-trip untouched. Hand-editing the JSON would skip the generation bump
and the writer block; do not.

**Verify:**

```
skfleet describe node node-41 | grep -E '"role"|"generation"'
skfleet nodes
```

**A thing this step does NOT do:** it does not change
`spec.identity`. `node-41` carries `capauth:architect@skworld.io`, an `agent`
class identity, while `control` declares `capauthIdentityClass: operator`.
After `set-role` the node is bound to `control` while still presenting an
agent identity. **This is a known and accepted inconsistency for the duration
of an outage.** Nothing enforces the identity class at write time today, so it
does not block you. Do not try to fix it during the incident by editing
identities; that is a much larger change than a promotion and it is how a
one-hour outage becomes a one-day one. Record it and move on.

### Step 2.2b. Move the labels, or the promoted seat gets no work **[added by the drill]**

**Do not skip this. `set-role` is not the whole promotion.** This is the single
biggest thing the drill found, and by design nothing in Step 2.2's verify
catches it.

`skfleet set-role` writes `spec.role` and deliberately preserves labels
untouched. **The scheduler never reads `spec.role`.** `scheduler.feasible`
filters on labels only:

```python
for key, value in sorted(workload.node_selector.items()):
    if view.labels.get(key) != value:
        return f"selector mismatch ({key}={value})"
```

Measured 2026-08-16:

| node | labels | `spec.role` |
|---|---|---|
| `node-noroc2027` | `always-on`, `control-plane`, `dev-primary`, `pi-harness`, `skcode-harness` | `control` |
| `node-41` | `heavy-build`, `pi-harness`, `skcode-harness` | `builder-standby` |

`.41` has **neither** `always-on` nor `control-plane`. **26 fleet objects
select on them**: 17 on `always-on` (`skgateway`, `skchat-daemon`, `skcomms`,
`skmemory-daemon`, `skingest`, `skchat-coturn`, `skchat-piper-tts`,
`skchat-nostr-relay`, the telegram and webui bridges, and 7 `skmem-*` /
backup / housekeep cronjobs) and 9 on `control-plane` (`skcapstone-daemon`,
`skos-scheduler`, `capauth-keystore`, `skgateway-claude-wrapper`,
`autopilot-daily`, `capauth-custody-doctor`, `skcapstone-backup-gfs`,
`skgateway-parity-check`, `skos-morning-brief`).

After Step 2.2 alone, all 26 have exactly one candidate node and it is the dead
one. Drilled, on a scratch fleet mirroring the `control-plane` selector:

```
node-drill-standby role=control (generation 6)
  node-drill-control   phase=Dead   role=control         -> not Ready (phase=Dead)
  node-drill-standby   phase=Ready  role=control         -> selector mismatch (control-plane=true)
  feasible nodes: NONE
```

`skfleet nodes` prints `role=control` throughout and looks completely correct.

**Use `skfleet label`. It merges.**

```
skfleet label node-41 control-plane=true always-on=true
```

That is the whole step. Every other field of the spec (`taints`, `cordoned`,
`address`, `identity`, `role`) is preserved, and the generation bumps by one.

**Do NOT use `skfleet apply` for this.** `apply` replaces the entire spec from
the document you hand it, so a minimal label-only document silently drops
`taints`, `cordoned` and `address`. Drilled: a label-only apply on a cordoned,
tainted node dropped both and **exited 0 with no warning, un-cordoning it**.
That is what this verb exists to prevent, and it is why the step above is one
line instead of the copy-the-whole-document dance this runbook used to
prescribe.

**Revert:**

```
skfleet label node-41 --remove control-plane --remove always-on
skfleet set-role node-41 builder-standby
```

Removing a label that is not set is a silent no-op, so the revert is safe to
run twice, and safe to run when you are unsure how far the promotion got.

**Verify, and this is the check that actually proves the promotion landed:**

```
skfleet describe node node-41 | jq '.spec.labels, .spec.spec'
skfleet placements
```

`skfleet placements` is the real test. Every `control-plane` and `always-on`
workload should now show `.41` as a feasible candidate. If placements still
report the dead node or report nothing feasible, the labels did not move.

**Should `.158` lose its labels?** In Case A, yes eventually, but not now: it
costs a second spec write during the incident and the node is `Dead`, so the
phase filter already excludes it. Do it during fail-back, or leave it. In Case B
leave it alone, `.158` is alive and may take the seat back.

**How urgent is this today?** The label gap is real but currently LATENT for
cronjobs, and it is worth knowing which half is which so nobody relaxes about
the wrong one.

`cron_controller` does not place anything. Its module docstring says so
outright: "skscheduler wiring for CronJob placement is a [later card]", and it
reads "whatever placement record (if any) already exists". Measured on the live
tree: 22 placement records exist and **all 22 are `job` kind**, none are
`cronjob` or `service`. So `skcapstone-backup-gfs` is not being placed by the
scheduler at all right now, and its `control-plane` selector is not currently
gating anything.

That makes this a latent bug, not a live outage, with one important
consequence: the failure arrives on the day cron placement gets wired, not on
the day of the promotion, so it will not look related to either change. Move
the labels anyway. The step is one line and the cost of skipping it is a
control seat that silently schedules nothing.

Better still would be for `set-role` to move role-implied labels itself, so
the two can never drift apart. That is card `1859466e`, and it is a design
decision (labels are per-node facts, roles are shared manifests) rather than
a missing line of code.

### Step 2.3. The gateway is an address handoff, not a systemd handoff

`skgateway.service` is **already enabled and active on `.41`**, on `:18780`,
in violation of `builder-standby`'s `units.mustNot`. That violation is
pre-existing (see [control-unit-set.md](control-unit-set.md), finding 1) and
today it works in your favour: there is nothing to enable.

Each gateway binds `:18780` on its own host, so two machines never fight for
the port. What they share is the config, out of Syncthing-replicated
`~/.skcapstone/gateway/skgateway.yaml`, and the clients, which resolve one
address.

**So the step is: repoint clients from `.158`'s gateway to `.41`'s.**

```
# on .41, confirm it is answering before you send anyone to it
curl -s http://127.0.0.1:18780/v1/models | head
systemctl --user is-active skgateway.service
```

**Revert:** repoint clients back to `.158`. Because both gateways stay up
throughout, this revert is instant and costs nothing. That is the one good
thing about the pre-existing violation.

Do not disable `skgateway.service` on `.158` in Case B. A gateway nobody is
pointed at is harmless, and leaving it up means the revert is a config change
rather than a service start.

### Step 2.4. Install the missing control units on `.41`

You need 21 unit files. **Do not install all 21.** Install the two that make
the seat, verify, and stop:

```
# on .41: capauth-authz.service, copied from .158's definition
systemctl --user cat capauth-authz.service    # on .158, to copy the ExecStart
# ExecStart=%h/.skenv/bin/capauth-service --host 127.0.0.1 --port 8420
systemctl --user daemon-reload
systemctl --user enable --now capauth-authz.service

# skcapstone.service: file already present on .41, just disabled
systemctl --user enable --now skcapstone.service
```

**Revert:**

```
systemctl --user disable --now capauth-authz.service
rm ~/.config/systemd/user/capauth-authz.service
systemctl --user daemon-reload
systemctl --user disable --now skcapstone.service
```

`skcapstone.service` is the cheapest win in this runbook: the unit file is
already on `.41` in `disabled` state, so it is one command with a one-command
revert and no file to write.

**Verify:**

```
systemctl --user is-active capauth-authz.service skcapstone.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8420/v1/authz/decide
```

### Step 2.5. `skoperator.timer` LAST, and only after `.158`'s is off

**This is the step that can break the fleet. Do not run it out of order.**

Stop and check, on `.158` if it is reachable:

```
systemctl --user is-enabled skoperator.timer     # MUST be: disabled
systemctl --user is-active  skoperator.timer     # MUST be: inactive
```

In Case A, where `.158` is genuinely gone, this check is satisfied by the box
being off. In Case B it is satisfied by Step B1 and by you having read the
output rather than assumed it.

Then, on `.41`, install the timer and its service (copy both from `.158`'s
definitions; the timer is `OnBootSec=2min`, `OnUnitActiveSec=15min`,
`Persistent=true`) and:

```
systemctl --user daemon-reload
systemctl --user enable --now skoperator.timer
```

**Revert:**

```
systemctl --user disable --now skoperator.timer
rm ~/.config/systemd/user/skoperator.timer
systemctl --user daemon-reload
```

The revert is clean and it is the one you will use during fail-back. Removing
the unit file, not just disabling it, matters: `Persistent=true` means a
lingering enabled timer fires a missed run on the next boot, and `.41` is a
laptop that boots often.

**Verify:**

```
systemctl --user list-timers skoperator.timer
find ~/.skcapstone/fleet -name '*.sync-conflict-*'    # still only your P3 baseline
```

Wait one full timer cycle, 15 minutes, and re-run the conflict check. A new
conflict file here means two operators. If you see one: immediately
`systemctl --user disable --now skoperator.timer` on `.41`, and find the other
writer before doing anything else.

### Step 2.6. Unfreeze

```
skfleet unfreeze
```

**Revert:**

```
skfleet freeze --reason "backing out promotion"
```

Only now. Unfreezing lets convergence and the AI seat act again, and you want
them acting against a fleet with exactly one operator, not a fleet mid-handoff.

**Verify:**

```
skfleet nodes
skfleet node doctor --all
skfleet get cronjobs
```

`skfleet nodes` should show `node-41` with `role=control`. Heartbeats should
be fresh. In Case B, `.158` will still appear as `Ready`, which is correct and
expected: it is alive, it is just not the seat any more.

### Step 2.7. Restore the user-facing services (not urgent)

The 19 remaining absent units are chat, comms, voice and backup surfaces. They
are outages people notice, but they are not control-plane outages, and the
control plane is now working. Do them one at a time, each one
disable-on-`.158`-then-enable-on-`.41`, needing skvault for the tokens and
credentials. [control-unit-set.md](control-unit-set.md) has the per-unit
detail, ports and which ones are strict handoff.

**Revert, per unit:** disable on `.41`, remove the unit file, re-enable on
`.158`.

Stop for the night after Step 2.6 if you can. The remaining work is better
done rested, and each unit's revert is independent.

---

## How two control seats are actually prevented

Not by the code. Be clear-eyed about this, because a runbook that overstates
its safety net gets people hurt.

**What the code does enforce.** `store.write_spec` refuses any writer whose
role is not `operator`. `store.write_status` refuses a writer whose `node`
does not match the node it is writing status for, so `sknoded` is genuinely
safe in parallel. `store.set_frozen` and `store.write_plane_file` refuse any
writer with `agent_seat=True`, so the autonomous seat can neither unfreeze
itself nor edit the carve-out manifest listing its own guardrails.

**What the code cannot enforce.** `skfleet`'s `_operator()` constructs
`Writer(role="operator", node=self_node_name(), ...)` on any machine it runs
on, with no check against the fleet's own record of which node holds the
`control` role. Two machines both presenting `operator` both pass. The store
sees one role, not two boxes. And the transport underneath is Syncthing, which
does not lock, does not order, and resolves divergence by writing a
`.sync-conflict-` file and keeping one version.

**So the prevention is procedural, and these four are it:**

1. **Order.** Case B demotes `.158` (B1) before `.41` gains anything (2.5).
   There is no window with both enabled because the sequence does not contain
   one. This is the whole mechanism. Everything else is detection.
2. **`skoperator.timer` is the last thing enabled and the first thing
   disabled.** It is the only scheduled writer, so bracketing the promotion
   with it means the risky window is minutes of deliberate work, not hours of
   background timers.
3. **Conflict-file detection, with a baseline.** P3 records the pre-existing
   conflicts so a new one is visible. Steps B5, 2.5 and 2.6 re-check. **Read
   the next section before you trust this one.** The drill showed it catches
   far less than this list implied.
4. **The freeze, for everything except human authorship.** It stops the AI
   seat and the actuators cold. It does not stop `skfleet apply` from a
   terminal. Do not lean on it for what it does not do. **[drilled]**,
   negative-controlled both ways: with the fleet frozen and a placement
   deleted, `skfleet reconcile` refused to place it; unfrozen, the same command
   placed it immediately. All five human write verbs succeeded while frozen.

### The conflict-file check finds collisions, not second seats **[added by the drill]**

A conflict file is produced only when **both** sides changed the same file
since the last agreed version. That is a race, and a race is the *unlikely*
shape of a two-seat incident.

Two experiments, same two seats, same object:

| | writes | conflict files found |
|---|---|---|
| both seats write inside one sync interval | 2 | **1**, detector fires |
| Syncthing converges between the writes | **10** | **0** |

Ten operator writes from two different machines and the runbook's detector
reported nothing. Sequential generations, well-formed signed writer blocks, an
internally consistent store, and no artifact anywhere that a second seat ever
existed.

The interleaved case is the likely one. The fleet tree is 368K, Syncthing
converges on it in seconds, and `skoperator.timer` fires every 15 minutes.

Which means the advice in Step 2.5 needs reading carefully:

> Wait one full timer cycle, 15 minutes, and re-run the conflict check. A new
> conflict file here means two operators.

That sentence is **true**. Its converse is **false** and it is easy to read it
as though it were: no new conflict file does *not* mean one operator. Keep the
15-minute wait as a settling delay. Do not treat a clean `find` as an all-clear.

**Use this instead, and run it at B5, at 2.5, and at F6.** Every spec carries
`writer.node`. A spec written by a node that does not hold `spec.role: control`
is a second seat, and it needs no collision to appear:

```
python3 - <<'PY'
import json, pathlib, collections
root = pathlib.Path.home() / ".skcapstone/fleet"
seats = {p.stem for p in (root/"objects/node").glob("*.json")
         if ".sync-conflict-" not in p.name
         and json.loads(p.read_text()).get("spec", {}).get("role") == "control"}
by, bad = collections.Counter(), []
for p in sorted((root/"objects").rglob("*.json")):
    if ".sync-conflict-" in p.name: continue
    try: d = json.loads(p.read_text())
    except Exception: continue
    w = d.get("writer") or {}
    if w.get("role") != "operator": continue
    by[w.get("node")] += 1
    if w.get("node") not in seats:
        bad.append((str(p.relative_to(root)), w.get("node"), d.get("updatedAt")))
print("control seats:", sorted(seats) or "NONE")
print("operator writes by node:", dict(by))
for f, n, t in bad: print(f"  FOREIGN WRITE {f} <- {n} @ {t}")
print("clean" if not bad else f"*** {len(bad)} foreign operator write(s)")
PY
```

Two honest caveats:

- It reports the **current** writer per object. Each write overwrites the last
  one, so a foreign write that the real seat later overwrote is invisible.
  Better than the conflict check, still not proof.
- `store.write_spec` emits no event, so there is no append-only write history
  anywhere. After the fact, "were there two seats and for how long" cannot be
  answered from the store. Plan on catching it live, not in forensics.

### A conflict file in `objects/` is worse than an alarm **[added by the drill]**

**If you find a `.sync-conflict-` file anywhere under `objects/`, move it out of
the tree before you do anything else.**

`store.list_specs` globs `*.json` and `node_controller.node_views` keys the
result by the `name` field **inside** each file. A conflict file is a byte copy
of the loser, so it carries the same `name`, sorts after the real file, and
**wins the dictionary**. Drilled, with the conflict file removed and replaced as
a control:

```
on disk, the object Syncthing KEPT:   role=builder-standby  cordoned=true
skfleet describe (reads by path):     role=builder-standby  cordoned=true
skfleet nodes    (reads via glob):    role=control          (no CORDONED flag)
remove the conflict file:             role=builder-standby  CORDONED
put it back:                          role=control
```

So:

- `skfleet nodes` and `skfleet describe` will tell you different things about
  the same node in the same second. **`describe` is the one telling the truth.**
- Everything downstream of `node_views` reads the discarded version, including
  `scheduler.feasible`. In the transcript above a **cordoned node appears
  schedulable**.
- It affects every kind, not only nodes. `list_specs` backs `skfleet get`,
  `skfleet services`, and the controllers.

The two pre-existing conflicts recorded in P3 are under
`status/node-noroc2027/` and are **inert**, because the status tree is read by
exact path. The exposure is only in `objects/`, and a promotion is the most
likely way to create one.

```
# do this the moment you see one, before any further reads
mkdir -p ~/incident-conflicts
mv ~/.skcapstone/fleet/objects/**/*.sync-conflict-* ~/incident-conflicts/
```

Moving it out of the folder is enough. Read it there, decide which version
should win, and re-assert that version with `skfleet apply`. Do not leave it in
place "as evidence": while it is in `objects/` it is not evidence, it is the
state.

There is a fifth thing that is not a mechanism but is worth saying out loud:
**only one person promotes.** Two humans in two terminals is the same
split-brain with a slower clock. Say in the incident channel who is driving.

---

## The data-loss window, honestly

Syncthing is eventually consistent. There is no synchronous commit anywhere in
this design. So there is a window, and pretending otherwise helps nobody.

**What can be lost.** Any spec written on `.158` that had not replicated to
`.41` when `.158` stopped. In practice: object specs (`objects/**`) written by
`skoperator.timer` or by a human in the last sync interval, and status writes
from `.158`'s own `sknoded` in the same window.

**How big the window is.** Small, and here is why you can believe that. The
fleet tree is **368K** total ([control-bus-folder.md](control-bus-folder.md)),
individual objects are 1KB to 8KB, and Syncthing's default rescan on a small
folder with connected peers is seconds, not minutes. The realistic worst case
is **one Syncthing sync interval, seconds to a couple of minutes**, and only
for writes that happened inside it.

The exception, and it is the one that actually bites: **if Syncthing on `.41`
was already stopped or disconnected before the incident, the window is however
long it was disconnected.** That could be days. This is exactly what P2 is
for, and it is why `syncthing cli show connections` and its `at` timestamp are
in the preconditions rather than buried here.

**How to bound it, before you write anything.**

```
# on .41, the newest thing the replica knows about
find ~/.skcapstone/fleet/objects -name '*.json' -printf '%T@ %p\n' | sort -rn | head -5
```

The newest `updatedAt` in that set is your replica's horizon. Anything `.158`
wrote after it is at risk. If `.158`'s disk is readable at all, even from a
rescue boot or by pulling the disk, copy `~/.skcapstone/fleet/objects/`
off it **before** promoting and diff the two trees. Ten minutes of diffing
beats a week of wondering which spec went missing.

**What to do when you cannot bound it.** Promote anyway, and treat every spec
as suspect until re-verified. The fleet keeps running its converged shape
regardless, so the loss is of recent *intent*, not of running services.
Re-assert the specs you care about by hand. Deliberately re-writing a spec you
already wrote is cheap. Discovering three weeks later that a cronjob was
silently disabled is not.

**What is NOT at risk.** Running services on other nodes. Node status subtrees
(each node owns and rewrites its own). Anything already converged. The loss is
confined to recent writes to `objects/`, which is the smallest and most
re-creatable part of the system. That is not an accident; it is what the
368K-versus-19G folder split is for.

---

## Failing back to `.158`

**This is the step people forget, and it is the one that leaves two seats
live.** A `.158` that comes back is a `.158` running its old configuration,
which believed it was the control seat. `skoperator.timer` has
`Persistent=true`: after a period down, systemd fires the missed run
**immediately at boot**. If you did Case A and never demoted `.158` (because
it was gone, so why would you), the seat re-arms itself the moment the box
powers on, and you get two operators with nobody at a keyboard.

**Therefore: the fail-back sequence starts BEFORE `.158` is on the network.**

### F0. Freeze first

```
skfleet freeze --reason "158 returning, failing back per runbook-promotion.md"
```

**Revert:** `skfleet unfreeze`.

### F1. Bring `.158` up with no network, or with the SK units already off

If you can boot it single-user, offline, or with networking down, do that and
disable the writers before it ever reaches the tailnet:

```
systemctl --user disable skoperator.timer
systemctl --user disable skcapstone-dashboard.service
systemctl --user disable skos-web.service
```

**Revert:** `enable` the same three. That revert is Step F5, so this is not a
step you can skip and fix later.

**If you cannot boot it offline** (Proxmox console unavailable, remote hands,
no IPMI): boot it, then immediately ssh in and run the disables. Accept that
`skoperator` may fire once during that gap. Check for conflict files
afterwards and expect to find one. Knowing you took the risk beats discovering
it.

**[added by the drill] What that one firing actually costs: the promotion
itself.** This was drilled and the result is worse than "a conflict file".

`.158` boots without having synced, so it acts on its pre-outage view, in which
it is still the seat and `node-41` is still `builder-standby`. It writes
`node-41.json` at the **same generation number** `.41`'s promotion wrote,
because it never saw that write. Syncthing then resolves on mtime, and `.158`
just wrote, so `.158` wins:

```
### .158 powers back on, Persistent=true fires before Syncthing connects
  .158 wrote 2 specs before Syncthing connected.
### Syncthing connects:
  CONFLICT   objects/node/node-drill-standby.json (SEATA158 won)
### the state both boxes now share:
  node-drill-standby: gen=2 role=builder-standby  writer=node-drill-control
```

**The promotion is silently reverted.** `.41` is a standby again and the only
node claiming `role: control` is the box that just came back from an
unexplained outage, with nobody having decided that.

Generation does not save you: both writes are generation 2, both well formed,
both signed. The only trace is the conflict file, and per the conflict-file
section above that file is now **overriding** the object in `skfleet nodes`,
which will cheerfully report `role=control` for the node that was just demoted.

Therefore, if you could not boot `.158` offline:

1. Move any `objects/` conflict file out of the tree immediately (see above).
2. Re-check `skfleet describe node node-41`, not `skfleet nodes`.
3. If `spec.role` came back as `builder-standby`, **the promotion was
   overwritten**. Re-assert it with Step 2.2 and 2.2b before doing anything
   else, or consciously decide to fail back now.

This is the strongest argument in this runbook for taking the time to bring
`.158` up with networking down. Ten minutes of console access buys you out of
the entire failure mode.

**In Case A this is where you catch the trap.** Case A never ran Step B1,
because there was nothing to run it against. F1 is Case A's B1, delayed. Do
not skip it because "we never promoted `.158` back".

### F2. Let Syncthing converge, and read the result

Bring `.158` onto the network with the writers disabled. Let Syncthing settle.

```
syncthing cli show connections
find ~/.skcapstone/fleet -name '*.sync-conflict-*'
```

`.158` will pull the specs `.41` wrote while it was the seat, including
`node-41.json` with `role: control`. Compare the conflict list against your P3
baseline. Resolve any new conflict **by hand**, choosing the version `.41`
wrote, because `.41` was the authoritative seat for that period. Then delete
the conflict file so the next incident's baseline is clean.

**Revert:** none. This step only reads and reconciles.

### F3. Demote `.41`

**On `.41`, and before `.158` gains anything:**

```
systemctl --user disable --now skoperator.timer
rm ~/.config/systemd/user/skoperator.timer
systemctl --user daemon-reload
skfleet set-role node-41 builder-standby
```

**Revert:**

```
skfleet set-role node-41 control
# reinstall skoperator.timer per Step 2.5
```

Same rule as promotion, mirrored: the seat is off on the outgoing box before
it is on on the incoming one. Remove the unit file, do not merely disable it,
for the `Persistent=true` reason.

Also reverse Step 2.4 and any of 2.7 you did:

```
systemctl --user disable --now capauth-authz.service skcapstone.service
```

Leave `skgateway.service` and `sknoded.service` and `syncthing.service`
running on `.41`. The gateway was there before the incident and removing it is
a separate change; the other two are parallel-safe and required.

**Verify:**

```
systemctl --user is-enabled skoperator.timer     # on .41, expect: not-found or disabled
skfleet describe node node-41 | grep '"role"'    # expect: builder-standby
```

### F4. Re-verify `.158` before handing the seat back

```
skfleet node doctor --all
skfleet nodes
df -h ~/.skcapstone
```

If `.158` came back degraded rather than fixed, **do not hand the seat back
yet**. A seat on a box that is about to fail again is a second promotion in
your near future, at a worse hour. `.158` reports 6.9GB free disk and a
`MemoryPressure` reason of "4.1GB available" in normal operation, so it does
not have much headroom to lose. Check that the thing that killed it is
actually fixed.

**Revert:** none. Read-only. Staying on `.41` is a valid outcome, and one you
should be willing to choose.

### F5. Re-enable the seat on `.158`

```
systemctl --user enable --now skoperator.timer
systemctl --user enable --now skcapstone-dashboard.service
systemctl --user enable --now skos-web.service
```

Then re-enable whatever B2 and B3 turned off, and repoint gateway clients back
to `.158` per 2.3's revert.

**Revert:** disable the same list; `.41` is still capable of retaking the seat
via Step 2.5.

`node-noroc2027`'s object still carries `role: control` throughout, since
nothing in this runbook changes it, so there is nothing to set back. That is
deliberate: the fewer spec writes fail-back needs, the fewer chances it has to
conflict.

### F6. Unfreeze and confirm exactly one seat

```
skfleet unfreeze
skfleet nodes
skfleet node doctor --all
find ~/.skcapstone/fleet -name '*.sync-conflict-*'
ssh cbrd21@100.86.156.5 "find ~/.skcapstone/fleet -name '*.sync-conflict-*'"
```

Then the check that actually closes the incident, on **both** boxes:

```
systemctl --user is-enabled skoperator.timer
```

`.158`: `enabled`. `.41`: `disabled` or `not-found`. **One seat.** Write that
pair of outputs into the incident record. It is the only evidence that the
fail-back finished, as opposed to appearing to.

Wait one full 15-minute timer cycle and re-check the conflict lists one last
time before you close.

**[added by the drill] And run the `writer.node` audit, because the conflict
list is not enough.** Ten writes from two seats produced zero conflict files in
the drill. Paste the script from "The conflict-file check finds collisions, not
second seats" and confirm:

- `control seats:` lists exactly one node, and it is `node-noroc2027`.
- `operator writes by node:` shows writes from that node only, or shows `.41`
  writes you can account for from the promotion window.
- no `FOREIGN WRITE` lines.

Also confirm no `.sync-conflict-` file is sitting in `objects/` on either box.
If one is, `skfleet nodes` is reading it in preference to the real object, and
every check you just ran was against the wrong data.

---

## Drilling this

**[rewritten by the drill]** The old version of this section told you to
`cp -r ~/.skcapstone/fleet/* /tmp/drill-fleet/`. That command fails as written
(nothing creates the target directory), and more importantly it is the wrong
shape: it copies production into a scratch tree by hand, which is exactly the
pattern `skfleet drill` was built to make unnecessary.

Use the harness. It is structurally incapable of touching production: the root
is resolved before it is judged, the forbidden prefix is the whole sovereign
home, an ownership marker means it cannot adopt a tree it did not create, and
`SKFLEET_ROOT` is never read as the target.

```
skfleet drill create --root /tmp/promo-drill        # 3 profiles, 3 nodes, 1 service, seeded drift
export SKFLEET_ROOT=/tmp/promo-drill                # now every verb below is on the copy

skfleet nodes
skfleet node doctor --all
skfleet freeze --reason drill
skfleet drill kill-control --root /tmp/promo-drill  # ages the seat's heartbeat past DEAD_AFTER_S
skfleet set-role node-drill-standby control
skfleet unfreeze

skfleet drill teardown --root /tmp/promo-drill      # deletes the whole tree
```

`skfleet drill promote --root ... [--revert]` runs and undoes the harness's own
promotion, and it refuses to promote while the seat is still `Ready`, which is
the reflex worth building.

**Budget:** the whole store-side sequence, all 21 commands with every revert
executed, is about **6 seconds**. There is no reason not to run it before
touching production, and no reason not to re-run it after any change to
`store.py`, `node_controller.py` or `scheduler.py`.

**Still not drillable in a scratch fleet.** Every `systemctl --user` step
(B1-B4, 2.4, 2.5, 2.7, F1, F5). `SKFLEET_ROOT` has no systemd equivalent, so
these remain reasoning rather than capability. Drilling them needs a scratch
user account with its own `~/.config/systemd/user/` holding copies of the 21
unit files with `ExecStart=/bin/true`, or a throwaway VM for the F1 reboot.
That is an open follow-up.

**The most valuable thing to drill is still not the happy path.** It is P2
failing: practise deciding, with an incomplete replica in front of you, whether
to promote and eat the loss or wait and eat the downtime. That decision is the
hard part, and it is the one you do not want to be making for the first time.

The second most valuable is the two-seat window, and the way to build it is two
drill trees plus something that moves files between them with last-synced
conflict semantics. That is how the conflict-file detector was shown to miss
ten writes out of ten. See
[promotion-drill-2026-08-16.md](promotion-drill-2026-08-16.md).

---

## Command index

Every command in this runbook was verified to exist on 2026-08-16 by running
its `--help`. Nothing below is plausible-looking invention. The store-side ones
were additionally **executed** in the drill on the same day, with their reverts,
against a scratch fleet.

**One verb that does not exist and that this runbook needs: `skfleet label`.**
See Step 2.2b. Until it does, moving a label means `skfleet apply` with the full
spec re-stated, because `apply` replaces the whole spec and silently drops
anything the document omits.

| command | verified | effect |
|---|---|---|
| `skfleet nodes` | yes | read-only |
| `skfleet describe <kind> <name>` | yes | read-only |
| `skfleet node doctor [NAME] [--all] [--json] [--strict]` | yes | read-only, self-documented as report-only |
| `skfleet control-bus audit` | yes | read-only, self-documented as safe on any node |
| `skfleet get <cronjobs\|modelservers\|agents\|configs>` | yes | read-only |
| `skfleet placements`, `skfleet services` | yes | read-only |
| `skfleet freeze [--reason TEXT]` | yes | **writes** `objects/_freeze.json`, human-only |
| `skfleet unfreeze` | yes | **writes** `objects/_freeze.json`, human-only |
| `skfleet set-role <name> <role>` | yes | **writes** the node spec, bumps generation |
| `skfleet taint <name> KEY=VALUE:EFFECT` | yes (wave 3) | **writes** the node spec |
| `skfleet untaint <name> KEY` | yes (wave 3) | **writes** the node spec; absent key is a success |
| `skfleet cordon <name>` / `uncordon <name>` | yes | **writes** the node spec |
| `skfleet actuation <name> --enable/--disable` | yes | **writes** the node spec |
| `skfleet apply -f FILE` | yes | **writes** an object spec |
| `syncthing cli show connections` | yes | read-only |
| `syncthing cli show system` | yes | read-only |
| `skcapstone doctor [--fix] [--verbose]` | yes | read-only without `--fix` |
| `skvault unlock --word <word>` | yes | unlocks the vault |

`skfleet taint` and `skfleet untaint` are not used in the main sequence, and
that is on purpose: taints steer the **scheduler**, not the control seat, and
there is no `NoExecute` effect in this fleet
([travel-taint-runbook.md](travel-taint-runbook.md)), so tainting `.158`
during an incident moves no running work and stops nothing you needed stopped.
They are listed because reaching for them is a natural instinct here and
because knowing they will not help is worth more than the instinct. If you do
want `.158` to stop attracting new placements while you work:

```
skfleet taint node-noroc2027 outage=true:NoSchedule
skfleet untaint node-noroc2027 outage
```

Both are write-on-change and idempotent on the key, so the untaint is safe to
run unconditionally during fail-back.
