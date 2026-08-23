# ADR: the node role model (service role and state tier are two axes)

**Status:** Accepted.
**Epic:** `3bbf39ea`. **Card:** `e884151b`. Sibling ADR:
[adr-edge-device-class.md](adr-edge-device-class.md) (card `b89f76ca`).
**Scope:** the nor-cluster install (Chef's fleet). See "Applicability to the
chi cluster" below for what this does and does not say about Casey's.

This ADR records a decision the code already implements. The kind is
`src/skcapstone/fleet/profiles.py`, the schema reference is
[profiles.md](profiles.md), and the four manifests that encode the decision
ship in `deploy/fleet-objects/profile/`. Nothing here is aspirational, and
nothing in this layer actuates: profiles are declarative and the drift report
is report-only.

## Context: one dial where there were always two

A node answers two questions that have nothing to do with each other. What
runs here, and how much sovereign state lives here. The fleet had a single
answer slot for both, so every node that wanted the first also got the second.

`profiles.py` states the separation in its own module docstring, and the two
axes land in different places in the object:

| axis | question | where it lives |
|---|---|---|
| service role | what runs here | the profile object's **name** |
| state tier | how much sovereign state lives here | `spec.stateTier` |

Neither is derived from the other, and both directions of the independence
have a real example. `builder-standby` runs almost nothing while holding a
complete replica. `worker-gpu` runs the heaviest workloads in the fleet while
holding nothing at all. A single "how much of a node is this" dial cannot
express either one, which is why `stateTier` has **no default** in
`normalize_profile_spec()`: a profile that will not say how much state it
holds is one nobody should converge against.

### The concrete failure this prevents

.100 is a GPU box whose job is to serve inference. It ended up carrying 5.0G
under `~/.skcapstone`, 3.4G of that `agents/` across 29 agent directories,
plus SK source checkouts hundreds of commits stale
([profiles.md](profiles.md), [control-bus-folder.md](control-bus-folder.md)).
Nobody decided that. "Runs inference" was treated as implying "is a full node,
so hosts all sovereign state", and the reason it was treated that way is that
no other shape existed: joining the control plane meant joining the one
`skcapstone-sync` Syncthing folder, which measures **19G** on the control
node, and the fleet store inside it measures **368K**. A node had to accept
0.002 percent of what it needed bundled with everything else.

That is the whole argument, and it is worth stating as a general rule rather
than as a .100 anecdote. **When the only way to get a small thing is to accept
a large one, the large one stops being a decision.** Separating the axes is
what lets a node say "serve inference, hold nothing" out loud.
[control-bus-folder.md](control-bus-folder.md) carries the measurements and
the scoped `skfleet-control` folder that fixes the transport half.

## Decision: four roles, and state tiers stated per role

| role | node | state tier | identity class | sync folders |
|---|---|---|---|---|
| `control` | .158, `node-noroc2027` | `full-replica` | `operator` | `skcapstone-sync`, `skfleet-control` |
| `builder-standby` | .41, `node-41` | `full-replica` | `agent` | `skcapstone-sync`, `skfleet-control` |
| `worker-gpu` | .100 | `none` | `worker` | `skfleet-control` |
| `observer` | norpv1300 | `none` | `observer` | (none) |

Every cell above is read from the shipped manifests, not from intent.
`full-replica` is exactly `{control, builder-standby}` and the manifests
declare `none` for both `worker-gpu` and `observer`;
`tests/fleet/test_profile_manifests.py` pins that set so a fifth role or a
quietly changed tier fails a test before it reaches a node.

A note on `worker-gpu`, because two documents currently disagree. The shipped
`deploy/fleet-objects/profile/worker-gpu.json` declares `"stateTier": "none"`
with `"syncFolders": ["skfleet-control"]`, the generator emits exactly that,
and the test asserts it. [profiles.md](profiles.md)'s tier table and worked
example still show `control-bus` for .100, which was the earlier draft of the
same idea (join the control bus, hold nothing sovereign). **The manifest is
authoritative**, and the distinction the two spellings were reaching for is
worth keeping: joining `skfleet-control` is not holding sovereign state, so
`none` is the honest tier and `control-bus` describes the transport, not the
tier. `profiles.md` should be corrected to match the artifact.

### Why builder-standby is a warm replica and not a hot mirror

.41 holds a second copy of the state and is the promotion target. It does not
run the control-plane loops, and its manifest forbids them by name:
`skgateway.service`, `skoperator.timer`, `skcapstone-dashboard.service` and
`skos-web.service` sit in its `units.mustNot`.

The reason is physical, not stylistic. .41 is a laptop and it sleeps. A
service that must be always-on cannot be honored by a machine that is not
always on, so a symmetric second seat would not be a second seat: it would be
a seat that is right most of the time and wrong without warning. Two seats
writing the same fleet files is also precisely the single-writer violation the
store's ownership guard exists to prevent (`store.write_spec` rejects any
writer role but `operator`, and a transport cannot enforce that, it can only
avoid breaking it).

The rule the fleet runs on is therefore **two copies of the STATE, one copy of
each running SERVICE.** The half-alive duplicate is not redundancy. It is the
proven source of the comms pileups, the oomd freezes and the outbox floods
this fleet has already lived through, and every one of those incidents was a
second copy of a service that was neither reliably up nor reliably down.

### The edge-device verdict, in one paragraph

A phone, a security key or a laptop that only authenticates is not a node
role, and adding a fifth profile for it would be a category error. Such a
device runs no `sknoded`, joins no fleet-store folder, and never has a spec
delivered to it: it authenticates and attests, and it never converges. It is a
capauth **device class**, which is a property of a credential, while a role is
a property of a machine the fleet installs software onto. The full reasoning,
the two device registries that exist today, and the fail-open versus
fail-closed asymmetry between them are in the sibling ADR,
[adr-edge-device-class.md](adr-edge-device-class.md) (card `b89f76ca`).

## Accepted risk: the single management seat is a SPOF, deliberately

.158 is the only control seat. `capauthIdentityClass: operator` appears in
exactly one manifest and a test asserts it stays that way. If .158 is lost,
the fleet keeps running whatever is already converged, and nobody can write a
new spec until a human promotes a replacement. That is a real single point of
failure and it is accepted, not overlooked.

It is accepted because the alternatives are worse in this fleet. A second
always-on control seat would need a third always-on box to break ties, which
is the same reason Nomad and every other Raft-backed control plane asks for
three servers rather than two: two seats do not give you availability, they
give you a split brain with better uptime numbers. The fleet has one
always-on box, one laptop, one GPU VM and one hypervisor. The laptop can never
be the third box, for the sleep reason above, so "make control HA" is not a
configuration change, it is a hardware purchase.

The mitigation is therefore a warm replica plus a **drilled** promotion
runbook rather than hot duplicates: card `591d2b1a` (promotion runbook and
drill, .41 to control on .158 loss) with `0afa9ffb` requiring a documented
revert on every step, drilled against a scratch fleet store and never against
production. Until that runbook has actually been run, the mitigation is a
plan, not a capability, and this ADR should be read as accepting the SPOF with
that caveat attached rather than as claiming it is already covered.

## norpv1300 stays explicitly unmanaged (card `6a38bae0`)

norpv1300 is the Proxmox hypervisor that hosts the GPU VM. The decision is
that it stays **unmanaged**: no SK packages, no `sknoded`, no fleet-store
membership, no Syncthing folder. Putting SK software on the box that hosts the
node adds risk for near-zero benefit, and the risk is asymmetric in the way
that matters: a mistake on a node costs you a node, while a mistake on the
hypervisor costs you the node and the hypervisor together. If an inventory of
it is ever wanted, the ssh-pull fallback in
[control-bus-folder.md](control-bus-folder.md) collects it from the outside
without installing an agent on it.

The `observer` role exists so that this decision is written down rather than
inferred from an absence, and the shipped
`deploy/fleet-objects/profile/observer.json` already encodes it:
`units.required` is `[]`, `packages.required` is `[]`, `syncFolders` is `[]`,
`stateTier` is `none`, and the description states in capitals that **NO
INSTALLATION EVER TARGETS AN OBSERVER NODE**, closing with the sentence "This
manifest exists so that 'not managed' is a recorded decision rather than an
omission." Its `unitsIgnore` is the single pattern `*`, so the profile takes
no position on anything running there, and its `packages.mustNot` still names
`capauth`, `cloud9`, `skcapstone`, `skcomms` and `skmemory`, so an accidental
install shows up as a finding.

"Observer" therefore means watched from the outside, never installed into. It
is the role that asserts nothing with teeth except the prohibition, and that
is the point.

## Applicability to the chi cluster

Roles are per-install **data**. The profile **code** travels via git.

`chi*` is Casey's sovereign install and `nor*` is Chef's; the two share code
through git and never share files. Nothing in this ADR obliges the chi cluster
to adopt these four names. The kind, the validator, the drift report and the
generator arrive there with any ordinary `skcapstone` upgrade, and the
manifests in `deploy/fleet-objects/profile/` describe nor-cluster boxes by
name (.158, .41, .100, norpv1300), so they are examples there rather than
policy. Casey can write his own manifests against the same schema, or bind no
node to a role at all, in which case `skfleet get profiles` shows `-` in the
NODES column and the drift report produces no findings. Adoption is opt-in per
install, which is the only shape that respects two sovereign clusters.

## Consequences

Good: a node can now decline state without declining membership, which is the
capability .100 never had. The tier is explicit on every profile, so a future
node that quietly acquires 5G of agent memory is a drift finding rather than a
discovery. And the SPOF is on the record, which means the promotion drill is
scheduled work rather than something remembered during an outage.

Costs, honestly: four roles is a taxonomy, and taxonomies attract fifth
entries. The test that pins `EXPECTED_ROLES` is there to make adding one a
deliberate edit. The single control seat remains a real availability limit
until hardware changes. And the state-tier promise is only as good as the
transport that implements it, which is the scoped folder work tracked in
[control-bus-folder.md](control-bus-folder.md), not something this ADR
delivers by itself.

## See also

- [profiles.md](profiles.md): the Profile kind's schema reference.
- [control-bus-folder.md](control-bus-folder.md): the 19G versus 368K
  measurements, the folder split, and the ssh-pull fallback.
- [adr-edge-device-class.md](adr-edge-device-class.md): why an auth-only
  device is a capauth device class and not a fifth role.
- [node-100-disposition.md](node-100-disposition.md) and
  [node-41-disposition.md](node-41-disposition.md): the per-unit dispositions
  the role manifests were generated against.
