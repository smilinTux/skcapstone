# Fleet install profiles: the two orthogonal axes

Epic `3bbf39ea`, card `6eb3adce`. Schema reference for the `Profile` kind
(`src/skcapstone/fleet/profiles.py`).

> **This layer performs ZERO actuation.** Profiles are declarative. The drift
> report is report-only. Nothing in this layer installs, uninstalls, enables
> or disables anything, and nothing in it ever will: installing remains a
> human action taken with the report in hand. A bug here can produce a wrong
> *finding*. It cannot produce a wrong *change*.

## Why two axes and not one

A node has two independent properties, and the fleet has been treating them
as one:

| axis | question it answers | field |
|---|---|---|
| **service role** | what runs here | the profile object's **name** |
| **state tier** | how much sovereign state lives here | `spec.stateTier` |

Neither is derived from the other. `builder-standby` runs almost nothing
while holding a full replica of everything. `worker-gpu` runs the heaviest
workloads in the fleet while holding nothing at all. A single "how much of a
node is this" dial cannot express either of them.

### The failure this prevents

.100 is a GPU box whose job is to serve inference. It ended up carrying 5.0G
of sovereign state across 29 agent directories, plus SK source checkouts
hundreds of commits stale, because "runs inference" was treated as implying
"is a full node, so hosts all sovereign state". Nobody decided that. It was
the only shape available: joining the control plane meant joining the one
19G Syncthing folder, all or nothing. See
[control-bus-folder.md](control-bus-folder.md) for the measurements and the
scoped-folder design that fixes the transport half.

Separating the axes is what lets a node say *serve inference, hold nothing*.

## The three state tiers

| tier | meaning | nodes |
|---|---|---|
| `full-replica` | holds the complete sovereign tree: agents, memory, sessions, backups | **.158** (`node-noroc2027`, control) and **.41** (`node-41`, builder-standby, the warm replica and promotion target) |
| `control-bus` | holds the fleet store only, a scoped folder budgeted under 10MB | **.100** (`ollama-gpu`, worker-gpu) |
| `none` | holds no SK state and runs no node agent | **norpv1300**, the hypervisor, deliberately unmanaged |

Two copies of the STATE, one copy of each running SERVICE. .41 is a warm
replica, not a hot mirror: a laptop that sleeps cannot honor symmetric
always-on services, and the half-alive duplicates are the proven source of
comms pileups, oomd freezes and outbox floods.

norpv1300 appears in this table so that "not managed" is a recorded decision
rather than an omission. Putting SK software on the box that hosts the GPU VM
adds risk for near-zero benefit.

## Field reference

Every field below is produced by `normalize_profile_spec()`. The two fields
that carry real consequence have **no default**: a profile that will not say
how much state it holds or what credential it carries is one nobody should
converge against.

| field | type | default | meaning |
|---|---|---|---|
| `description` | str | `""` | human-readable summary of the role |
| `packages` | name-list block | all three empty | distributions this role may have installed |
| `units` | name-list block | all three empty | systemd `--user` unit names this role may have enabled |
| `unitsIgnore` | list of str | `[]` | fnmatch patterns for units the profile takes no position on, so a desktop box full of `gpg-agent*.socket` does not read as drift |
| `stateTier` | enum | **required** | `full-replica` \| `control-bus` \| `none` |
| `capauthIdentityClass` | enum | **required** | `operator` \| `agent` \| `worker` \| `observer` |
| `syncFolders` | list of str | `[]` | Syncthing folder ids a node of this role joins. A REFERENCE only: see "Sync folders own their ignore rules" below |
| `deleted` | bool | `false` | tombstone: stops management, never uninstalls anything |

A **name-list block** is `{required, allowed, mustNot}`, each a list of
non-empty strings. Entries are sorted and de-duplicated on normalization, so
two manifests that mean the same thing compare equal.

Two contradictions raise `ProfileSpecError` rather than resolving silently,
because either one would make the drift verdict non-deterministic:

1. A name in both `allowed` and `mustNot`. It cannot be permitted and
   forbidden at once.
2. A name in `required` but not in `allowed`. Requiring what you do not allow
   is the same contradiction one step out. It is **not** auto-widened: the
   manifest has to say what it means.

Empty name lists mean "this profile asserts nothing", which the drift report
renders as no findings. They never mean "remove everything".

Enum members are exported as `profiles.STATE_TIERS` and
`profiles.IDENTITY_CLASSES` for callers that need to validate against them.

## Worked example

`deploy/fleet-objects/profile/worker-gpu.json`:

```json
{
  "kind": "profile",
  "name": "worker-gpu",
  "labels": {"epic": "3bbf39ea"},
  "spec": {
    "description": "Serve fleet inference. Hold zero sovereign state.",
    "units": {
      "required": ["skai-beellama.service"],
      "allowed": [
        "comfyui.service",
        "f5-tts.service",
        "qwen3-arc.service",
        "skai-beellama.service",
        "syncthing.service",
        "whisper-stt.service"
      ],
      "mustNot": ["skchat-daemon.service", "skcomms.service", "sknoded.service"]
    },
    "unitsIgnore": ["gpg-agent*.socket", "dirmngr.socket", "keyboxd.socket"],
    "stateTier": "control-bus",
    "capauthIdentityClass": "worker",
    "syncFolders": ["skfleet-control"]
  }
}
```

Apply it:

```console
$ skfleet apply -f deploy/fleet-objects/profile/worker-gpu.json
applied profile/worker-gpu (generation 1)

$ skfleet get profiles
NAME        STATE-TIER   IDENTITY-CLASS  REQUIRED  MUSTNOT  NODES
worker-gpu  control-bus  worker          1         3        -
```

A malformed profile is rejected at apply time and never reaches disk:

```console
$ skfleet apply -f broken.json
Error: invalid profile spec: unknown stateTier 'half-replica'
  (known: ['control-bus', 'full-replica', 'none'])
```

Manifests live in `deploy/fleet-objects/profile/*.json` alongside the other
real loadable fleet objects (`deploy/fleet-objects/bulletproof/cronjob/`).
This document is the schema **reference**, not a second home for the
manifests. Two homes for one truth is the drift these manifests exist to
prevent (decision card `c5ad2471`).

## How a node binds to a profile

A node carries `spec.role`, whose value is a profile object's name:

```console
$ skfleet describe node node-100 | jq .spec.spec.role
"worker-gpu"
```

`skfleet get profiles` reads that field to fill its `NODES` column. A node
object with no `spec.role` binds to nothing and shows as `-`, which is the
correct reading before every node has been backfilled.

The `spec.role` field and its `set-role` operator action are owned by card
`8258517f`; admission requiring it and the live backfill are card `fdd17a01`.

## The converge-side profile gate (`SKFLEET_PROFILE_GATE`)

sknoded can consult a node's profile before it heals a unit. The rollout
flag has the same three-mode shape as `SKFLEET_SIGNING`:

| value     | behavior                                                        |
| --------- | --------------------------------------------------------------- |
| `off`     | default. The gate is never consulted, nothing changes.          |
| `shadow`  | emits a `Degrade` / `OutsideProfile` event and condition only.  |
| `enforce` | additionally refuses to **heal** a unit the role forbids.        |

`enforce` never issues a stop verb. Refusing to heal is the entire
enforcement: taking a running service down because a manifest disagrees with
it would turn documentation debt into an outage, which is the failure mode
this epic exists to avoid.

Only `units.mustNot` denies. A unit no manifest mentions is permitted, and so
is every unit when the role is unbound, the role is unknown, or the manifests
are missing, unreadable or invalid. A gate that failed closed on a file that
has not synced yet would stall services mid-install.

Manifests are read from the first readable source of:
`$SKFLEET_PROFILE_MANIFESTS` (authoritative when set), the fleet tree's
`objects/profile/`, then the shipped `deploy/fleet-objects/profile/` in a
source checkout.

## Sync folders own their ignore rules, roles do not

`syncFolders` says which Syncthing folders a role joins. It deliberately does
NOT say what those folders must ignore, and the Profile kind is the wrong
place to put that.

`~/.skcapstone/.stignore` opens with `*.key`, `*.pem` and `**/private.*`.
Those three lines are the only reason the control node can hold eleven
`agents/*/capauth/identity/private.asc` files while a peer in the same
`sendreceive` folder holds zero: Syncthing never scans or announces an
ignored file, so the source never offers it. Every node in the folder needs a
byte-identical answer, or the no-secrets invariant becomes per-node.

A Profile is role-keyed, so a ruleset kept there would be per-role, and two
roles joining one folder could disagree. So the ruleset is keyed by FOLDER ID
and lives with the folder definition:

- built-in floor: `DEFAULT_RULESETS` in `skcapstone/fleet/stignore_doctor.py`
- optional object: `syncfolder/<folder-id>.json`, with `spec.root`,
  `spec.requiredIgnores` and `spec.recommendedIgnores`

An object may only ADD to the built-in floor. It cannot drop a rule, for the
same reason `syncthing_setup._write_stignore` merges by union and never
overwrites: ignoring more costs a file that does not replicate, ignoring less
leaks private keys onto every peer.

To check a node:

```console
$ skfleet node stignore            # report only, writes nothing
$ skfleet node stignore --strict   # exit 1 on an error-grade finding
```

A missing required rule, or a folder with no `.stignore` at all, grades
`error`. A missing narrower credential rule grades `warn`, because it covers
a subsystem the node may simply not run. Folders whose root is not on this
host are skipped: a folder a node does not hold cannot leak through it.

This is a SIBLING of `skfleet node doctor`, not part of it. `doctor` diffs a
role profile against a published inventory and skips any node with no role
bound; this invariant is folder-keyed and applies to a role-less node exactly
as much as to a control node.

## Discovering this kind at runtime

```console
$ skfleet explain profile --json
```

returns the same field set described above, so an operator who has never read
this file can still find the schema from the CLI.

## What this layer does NOT see

The profile layer observes **systemd units and installed SK packages, and nothing
else**. `fleet/nodeinventory.py` runs exactly two commands, `systemctl --user
list-unit-files --state=enabled` and its system-scope equivalent, plus a read of
installed distribution metadata. Anything not started by systemd is invisible to
it, and therefore invisible to `skfleet node doctor`.

This is not hypothetical. On `.100` today:

```console
$ docker ps --format "{{.Names}}  {{.Image}}"
frigate  ghcr.io/blakeblackshear/frigate:stable

$ systemctl --user list-unit-files --state=enabled | grep -ci frigate   # 0
$ systemctl        list-unit-files --state=enabled | grep -ci frigate   # 0
```

Frigate is healthy, has been up for months, and exposes ports 5000, 8554-8555 and
8971. The profile layer cannot see any of it. A `.41` investigation found the same
shape with ingress: a `cloudflared` deployment running as k3s pods, owned by no
unit, positioned to reach services the systemd tunnel could not.

**Why this matters more than a missing feature.** A drift report that says nothing
about containers reads exactly like a drift report that found nothing wrong. The
silence currently means "no systemd drift" while looking like "no drift". A public
ingress or a network-exposed workload is precisely the class of thing a
`worker-gpu` or `builder-standby` profile would want to forbid, and it is the class
this layer is blind to.

So read a clean `node doctor` as: **this node's systemd units and SK packages match
its profile.** It is not a statement about containers, k3s workloads, or anything
else started by another supervisor.

Card `7892e416` holds the decision on whether to extend the observation surface to
workload-managed units or to keep this scope and rely on this paragraph. Documenting
the limit is the cheap half and is done here; widening the surface is the expensive
half and is not.

## See also

- [control-bus-folder.md](control-bus-folder.md): the Syncthing folder split
  and the ssh-pull fallback, which is how a `control-bus` node receives fleet
  specs without receiving 19G of sovereign state.
- [services/](services/): the Service kind manifests, the closest existing
  precedent for a declarative fleet object.
- [../runbooks/fleet-cold-start.md](../runbooks/fleet-cold-start.md): bringing
  a fresh box from bare to admitted, which is where a node first acquires the
  role this document describes.
