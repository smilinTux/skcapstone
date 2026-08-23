# Travel taint runbook (`node-41`)

Epic `3bbf39ea`, card `5e11d880`. Companion to
[profiles.md](profiles.md) and the scheduler filter in
`src/skcapstone/fleet/scheduler.py`.

`node-41` is the `builder-standby` box and it is a laptop, so it is the one
node in the fleet whose availability is a function of where it physically is.
When it travels, it should stop attracting new work without being deleted,
cordoned by hand, or forgotten in a cordoned state for a week.

> **The systemd half of this document is NOT applied.** Nothing below the
> "Proposed sleep hook" heading exists on `node-41` today. The CLI half (the
> `taint` and `untaint` verbs) IS merged and usable. Every proposed step
> carries its own revert line, so applying it is reversible one step at a
> time.

## What the scheduler actually does with a taint

Read `scheduler.feasible()` and `scheduler.select()` before choosing an
effect, because only two exist here and they behave very differently:

| effect | where it is honored | behavior |
|---|---|---|
| `NoSchedule` | `feasible()`, the hard filter | The node is EXCLUDED from placement unless the workload carries a matching toleration. |
| `PreferNoSchedule` | `select()`, the ranking key | The node stays feasible but sorts after every non-avoided candidate, so it is chosen only when nothing else can take the work. |

There is no `NoExecute`. Nothing in this fleet evicts an already-running
workload, so `set_taint()` rejects that effect rather than writing policy
that reads like eviction and does nothing. A workload tolerates a taint by
key plus value, or by key alone to tolerate any value of that key
(`Workload.tolerations`, `scheduler._tolerated()`).

**Which effect for travel:** `NoSchedule` when the box is expected to be off
the network (suspended in a bag), `PreferNoSchedule` when it is merely
unreliable (tethered, on battery) and should still be a last-resort
candidate. The hook below uses `NoSchedule`, because a suspended laptop can
take no work at all.

## The verbs

Both run on the control-plane node, from the operator seat, against the
fleet tree the control plane owns.

```
skfleet taint   node-41 travel=true:NoSchedule
skfleet untaint node-41 travel
```

Properties worth knowing before you wire them into anything automatic:

- **Idempotent on the key.** Re-tainting `travel` replaces that entry in
  place, it never appends a second one. Two entries sharing a key would make
  `feasible()` depend on list order.
- **Write-on-change.** Re-asserting a taint that is already exactly present,
  or clearing a key that is not there, writes nothing and does not bump the
  generation. This matters because the fleet tree is a Syncthing folder: a
  hook that fired on every resume would otherwise fan a no-op write out to
  every node, forever.
- **Every other spec field survives.** `role`, `cordoned`, `address`,
  `identity` and `actuate` round-trip untouched, because the setter overlays
  one field onto the current spec and rewrites through `store.write_spec`
  rather than editing the JSON. Hand-editing the file would bypass both the
  generation bump and the SPE writer block.
- **`untaint` of an absent key is a success**, printing `nothing to do`. The
  resume path must be safe to run unconditionally.

Verify either way with `skfleet describe node node-41` and read
`.spec.spec.taints`.

## Manual use (available now)

Before travel, on the control node:

```
skfleet taint node-41 travel=true:NoSchedule
skfleet describe node node-41   # confirm the taint and the new generation
```

On return:

```
skfleet untaint node-41 travel
skfleet describe node node-41   # taints back to []
```

**Revert:** `skfleet untaint node-41 travel`. That is the whole revert. No
file is left behind and no other field was touched.

## Proposed sleep hook (NOT APPLIED)

The intent is that the laptop taints itself on suspend and clears the taint
on resume, so no human has to remember either half. It is written here
rather than installed because applying it is a change to a live machine that
other sessions may be using, and because it deserves its own change record.

### Precondition

The hook writes spec, and only the operator seat may write spec
(`store.write_spec` raises `OwnershipError` otherwise). `node-41` is not the
control-plane node, so decide the seat question before installing anything:
either the unit runs `skfleet` over ssh against the control node, or the
control node runs a small watcher on `node-41`'s heartbeat age instead. The
ssh form is written below because it keeps the authority on the control
node, where it already lives.

### Step 1: the hook unit

Path on `node-41`: `/etc/systemd/system/fleet-travel-taint.service`

```ini
[Unit]
Description=Taint this node in the fleet across suspend (epic 3bbf39ea)
Before=sleep.target
StopWhenUnneeded=yes

[Service]
Type=oneshot
RemainAfterExit=yes
# Absolute paths only: systemd units get almost no PATH, which is how
# sk-alert silently never fired from a scheduler.
ExecStart=/usr/bin/ssh -o BatchMode=yes control-node /home/cbrd21/.skenv/bin/skfleet taint node-41 travel=true:NoSchedule
ExecStop=/usr/bin/ssh -o BatchMode=yes control-node /home/cbrd21/.skenv/bin/skfleet untaint node-41 travel

[Install]
WantedBy=sleep.target
```

`ExecStart` runs on the way into suspend and `ExecStop` on the way out,
which is what `StopWhenUnneeded=yes` plus `WantedBy=sleep.target` buys: one
unit for both edges instead of two that can disagree.

**Revert step 1:**

```
sudo systemctl disable --now fleet-travel-taint.service
sudo rm /etc/systemd/system/fleet-travel-taint.service
sudo systemctl daemon-reload
```

### Step 2: enable it

```
sudo systemctl daemon-reload
sudo systemctl enable fleet-travel-taint.service
```

**Revert step 2:** `sudo systemctl disable fleet-travel-taint.service`. The
unit file stays, nothing runs.

### Step 3: prove both edges before trusting it

```
sudo systemctl start fleet-travel-taint.service   # simulate the suspend edge
skfleet describe node node-41 | grep -A3 taints   # expect travel=true
sudo systemctl stop fleet-travel-taint.service    # simulate the resume edge
skfleet describe node node-41 | grep -A3 taints   # expect []
```

Only then try a real `systemctl suspend`. A unit that is enabled but has
never had both edges exercised is not evidence of anything.

**Revert step 3:** `skfleet untaint node-41 travel` if the box is left
tainted.

### Failure mode to watch for

If the resume edge fails (ssh unreachable on wake, which is the likely
case), the node stays tainted and quietly stops receiving work. That is the
safe direction, but it is invisible. Before this is applied, pair it with a
staleness check that alerts on a `travel` taint older than a day, or accept
that `skfleet describe` is the only thing that will tell you.
