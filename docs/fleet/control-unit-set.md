# The control unit set: what `.41` must gain to become the control seat

**Epic:** `3bbf39ea`. **Card:** `fe021dea`. **Companion:** the promotion
sequence lives in [runbook-promotion.md](runbook-promotion.md) (card
`0afa9ffb`); the decision this mitigates is in
[adr-node-role-model.md](adr-node-role-model.md).

Collected **read-only**. Nothing on `.41` or `.158` was enabled, disabled,
started or stopped to produce this document. See "Proof nothing was changed"
at the end.

## Why this document exists

The ADR accepts a single management seat as a deliberate SPOF and names the
mitigation: a warm replica plus a drilled promotion runbook. A runbook cannot
be drilled until somebody has written down what promotion actually consists
of, unit by unit. This is that list.

The useful question is not "which units does control run" (the manifest
answers that) but **"how much work is each one on the night it matters"**.
Enabling a unit whose file is already on disk is one command. Enabling a unit
whose file does not exist means finding it, writing it, sourcing its secrets
and its environment, and discovering at 3am which of those you got wrong. The
table below separates those two cases into their own column, because they are
not the same task and pretending otherwise is how a runbook lies to you.

## The short answer

`.41` needs **21 unit files it does not have**, needs to **enable 1 unit whose
file is already present** (`skcapstone.service`), and **already runs 6** of the
control set. Of the five units control declares `required`, `.41` has two
running (`sknoded.service`, `skgateway.service`), has one file present but
disabled (`skcapstone.service`), and has **no file at all** for the two that
actually make a machine the control seat: `capauth-authz.service` and
`skoperator.timer`.

That last pair is the honest headline. The 21 missing files are mostly chat,
comms and voice surfaces, which are user-facing outages, not control-plane
outages. **The control plane itself is `skoperator.timer` plus the identity it
writes with, and that is the smallest and least-installed part of the set.**

## What makes `.158` the control seat today

Three things, and only one of them is a unit.

**1. The `control` profile's `units.required`.** Read from
`deploy/fleet-objects/profile/control.json`, verified live on `.158`:

| required unit | `.158` enabled | `.158` active |
|---|---|---|
| `capauth-authz.service` | yes | yes |
| `skcapstone.service` | yes | yes |
| `skgateway.service` | yes | yes |
| `sknoded.service` | yes | yes |
| `skoperator.timer` | yes | yes |

**2. The role binding in the fleet store.**
`objects/node/node-noroc2027.json` carries `spec.role: control` and
`spec.identity: capauth:lumina@skworld.io`. `node-41.json` carries
`spec.role: builder-standby` and `spec.identity:
capauth:architect@skworld.io`. The profile manifests give `control` the
`operator` capauth identity class and `builder-standby` the `agent` class, and
`tests/fleet/test_profile_manifests.py` pins that so it cannot drift quietly.

**3. Nothing else.** There is no lock, no lease, no election. This is the part
that matters most for the runbook and it deserves stating plainly:

`src/skcapstone/fleet/cli.py` builds its writer as

```python
def _operator() -> store.Writer:
    return store.Writer(role="operator", node=self_node_name(), identity=store.writer_identity())
```

with no check on which machine it is running on. `store.write_spec` then
enforces `writer.role != "operator"` and nothing more. **Any machine with the
`skfleet` CLI and a copy of the fleet tree can write a spec today, and `.41`
has both.** The `skfleet` shim is on `.41` at `~/.skenv/bin/skfleet` and its
`~/.skcapstone/fleet/objects` tree is the same Syncthing-replicated tree as
`.158`'s. The single-writer invariant is currently held by convention, not by
code, and that is precisely the hazard the promotion runbook has to design
around.

## The delta table

`.41 file` is the answer to `systemctl --user list-unit-files <name>` on
`.41`: `enabled`, `disabled` (file present, not wanted), or **`ABSENT`** (no
unit file exists on the box). `.41` live state collected 2026-08-16 over
Tailscale; the recorded inventory is
[inventories/node-41-user-units.json](inventories/node-41-user-units.json).

| control unit | required? | `.158` | `.41` file | work to promote | parallel? |
|---|---|---|---|---|---|
| `capauth-authz.service` | **yes** | enabled | ABSENT | install unit, point at `capauth-service --host 127.0.0.1 --port 8420` | **safe** |
| `skcapstone.service` | **yes** | enabled | disabled | `enable --now` | **safe** |
| `skgateway.service` | **yes** | enabled | **enabled** | none, already running | **STRICT** |
| `sknoded.service` | **yes** | enabled | enabled | none, already running | **safe, and required on both** |
| `skoperator.timer` | **yes** | enabled | ABSENT | install timer + its service, plus the seat's config | **STRICT, the single-writer unit** |
| `skcapstone-dashboard.service` | no | enabled | ABSENT | install unit (`skcapstone dashboard --port 7778`) | **STRICT** |
| `skos-web.service` | no | enabled | ABSENT | install unit (`skos serve --port 7781`) | STRICT in practice |
| `skcot.service` | no | enabled | ABSENT | install unit, TAK identity + certs (`:8087`/`:8089`) | **STRICT** |
| `skcomms-api.service` | no | enabled | ABSENT | install unit (`uvicorn skcomms.api:app :9384`) | **STRICT** |
| `skcomms-signaling-broker.service` | no | enabled | ABSENT | install unit (`:9390`) | **STRICT** |
| `skchat-telegram-lumina.service` | no | enabled | ABSENT | install unit + bot token | **STRICT** |
| `skchat-telegram-opus.service` | no | enabled | ABSENT | install unit + bot token | **STRICT** |
| `skcode-hostd.service` | no | enabled | ABSENT | install unit, needs `SKCODE_HOSTD_TAILSCALE_IP` + host id (`:9394`) | **STRICT** |
| `skvoice.service` | no | enabled | ABSENT | install unit (`:18800`) | **STRICT** |
| `skchat-nostr-relay.service` | no | enabled | ABSENT | install unit + relay keys | **STRICT** |
| `skchat-piper-tts.service` | no | enabled | ABSENT | install unit + voice models | safe |
| `skchat-coturn.service` | no | enabled | ABSENT | install unit + TURN creds | safe |
| `skcomm-daemon.service` | no | enabled | ABSENT | install unit | **STRICT** |
| `skcomm-queue-drain.timer` | no | enabled | ABSENT | install timer + service | **STRICT** |
| `skcomm-heartbeat.timer` | no | enabled | disabled | `enable --now` | **safe by design** |
| `capauth-backup.timer` | no | enabled | ABSENT | install timer + `capauth-backup.sh` target | **STRICT** |
| `skchat-backup.timer` | no | enabled | ABSENT | install timer + backup destination | **STRICT** |
| `skchat-health-probe.timer` | no | enabled | ABSENT | install timer | safe |
| `skingest-maintain.timer` | no | enabled | ABSENT | install timer, needs `skingest` package | **STRICT** |
| `sksecurity-audit.timer` | no | enabled | ABSENT | install timer | safe, duplicates reports |
| `skcomms-access.service` | no | enabled | enabled | none | safe |
| `skchat-app-web.service` | no | enabled | enabled | none | safe |
| `skchat-daemon.service` | no | enabled | enabled | none | safe |
| `syncthing.service` | no | enabled | enabled | none, must never stop | **safe, required on both** |

Totals: **21 ABSENT**, **2 present-but-disabled** (`skcapstone.service`,
`skcomm-heartbeat.timer`), **6 already enabled**.

### Packages

Control's `packages.required` is `["skcapstone"]` and `.41` has it, so no
package blocks promotion outright. `.41` is nevertheless missing seven
packages that `.158` carries and control's `allowed` list names: `sk-pqc`,
`sk-pqc-rs`, `skcore`, `skguide`, `skingest`, `skrender`, `sktrip`. Only
`skingest` backs a unit in the table above (`skingest-maintain.timer`), so in
practice the package gap costs you the wiki maintenance sweep and nothing
else on promotion night. Install the rest afterwards, calmly.

## What cannot simply move

Some of these units are a copy. Some are a **role**, and a role has exactly
one holder. Moving a copy is easy and reversible. Moving a role is a handoff,
and the failure mode of getting it wrong is not downtime, it is two writers.

### `skoperator.timer` is the single-writer violation

This is the one the ADR is really about. The operator seat is the sole spec
writer. `store.write_spec` refuses any role but `operator`, but it authorises
by role, and both machines would present role `operator`. It has no way to
see that two different boxes are holding the same role at the same time, and
the fleet tree it protects is a Syncthing folder, so the two writers do not
even collide at the filesystem layer. They both succeed, and Syncthing
resolves the divergence by writing a `.sync-conflict-` file and picking a
winner by timestamp.

That is not a hypothetical. `~/.skcapstone/fleet/status/node-noroc2027/`
currently holds `heartbeat.sync-conflict-20260814-053908-CIHSBZ4.json` and
`node.sync-conflict-20260814-053918-CIHSBZ4.json`, and the fleet already
reports the condition: `SyncConflict / DoctorProbe / "2 conflict file(s) in:
status"` is `True` on **both** node objects right now. The mechanism that
would silently eat a spec write during a split-brain promotion is visibly
operating on this fleet today.

**`skoperator.timer` must never be enabled on `.41` while `.158`'s copy is
enabled.** Not "briefly", not "just to test". Strict handoff, and the runbook
makes it the last thing enabled on `.41` and the first thing disabled on
`.158`.

### `skgateway.service` is already running on both, and that contradicts the model

The `builder-standby` manifest lists `skgateway.service` in `units.mustNot`.
`.41` has it **enabled and active** right now, on `:18780`, with the same
`config-path.conf` drop-in as `.158` pointing at the Syncthing-shared
`~/.skcapstone/gateway/skgateway.yaml`. `skfleet node doctor` grades this
`error forbidden_units`, so the fleet knows; nobody has acted on it.

The nuance matters for the runbook. Each gateway binds `:18780` on **its own
host**, so there is no TCP port fight between two machines. What there is:

- Both read the same policy and config files out of the shared folder, so a
  policy edit made for one takes effect on the other with no announcement.
- Both hold provider secrets from their own `~/.config/skgateway/secrets.env`,
  so rate limits, budgets and the `skgateway-parity-check` cronjob see two
  independent consumers of one set of upstream quotas.
- Clients resolve one address. Whoever holds that address is the gateway.

So the handoff for `skgateway` is **not** a systemd handoff, it is an
**address handoff**, and the runbook must say so. Enabling the unit on `.41`
achieves nothing on its own because it is already enabled. Repointing clients
is the actual step, and the actual revert.

The pre-existing `mustNot` violation is called out here rather than fixed:
this card is read-only and the fix is a separate change with its own blast
radius. It is logged as a finding, not an action.

### The seat-shaped services

`skcapstone-dashboard.service` (`:7778`) and `skos-web.service` (`:7781`) are
the operator's hands. Two dashboards means two sets of buttons, each backed by
its own process, both writing the same store through the same `operator` role.
Every argument above about `skoperator.timer` applies to them with a human
clicking instead of a timer firing. Strict handoff, and disable them on `.158`
before enabling on `.41` in the degraded case.

`skcode-hostd.service` is the remote-control daemon. Two of them accepting
control on the tailnet is a second unattended door into the fleet, and the
one that is not being watched is the one that gets used. Strict handoff.

### The single-identity bridges

`skchat-telegram-lumina.service` and `skchat-telegram-opus.service` each poll
Telegram as one bot. Telegram hands a given update to one long-poll consumer.
Two bridges on the same token means messages arrive at whichever process won
the race, so the agent appears to answer some messages and ignore others, at
random, with nothing in either log looking wrong. This is worse than the
bridge simply being down, because being down is visible. Strict handoff.

Same shape, same reason: `skchat-nostr-relay.service` (one relay identity),
`skcot.service` (one TAK client identity), `skcomms-api.service` and
`skcomms-signaling-broker.service` (the federation `/inbox` and the signaling
rail are addressed, and a peer that reaches the wrong one gets a correct-looking
answer from a seat that is not authoritative).

### The write-somewhere-shared timers

`capauth-backup.timer`, `skchat-backup.timer`, `skcomm-queue-drain.timer` and
`skingest-maintain.timer` all write to a destination that is not local to the
node. Two copies of a backup timer racing on one destination is how you get a
half-written archive that both runs believe they finished. Two queue drains
deliver the same message twice or lose it to a double-claim. Strict handoff,
even though none of them are `required`, and precisely because none of them
are `required`: nothing will complain if you forget them.

## What IS safe to run in parallel

These can be up on both boxes through the whole promotion, and several of them
**must** be:

| unit | why parallel is fine |
|---|---|
| `sknoded.service` | `store.write_status` enforces `writer.node == node`, so each node can only write its own status subtree. Two nodeds cannot collide by construction. Required on both, always. |
| `syncthing.service` | The transport. Stopping it on either side is what creates the data-loss window, not what prevents it. Never stop it as part of a promotion. |
| `capauth-authz.service` | A stateless PDP bound to `127.0.0.1:8420`. Every host that gates anything needs its own. Two is correct, not a conflict. |
| `skcomm-heartbeat.timer` | Publishes with `--node-id %H`, host-unique by design. This is the fix for the 2026-07-23 outbox flood; running it on both is the intended shape. |
| `skchat-piper-tts.service`, `skchat-coturn.service` | Media helpers. Clients pick one. A second is spare capacity. |
| `skchat-health-probe.timer`, `sksecurity-audit.timer` | Read-only probes. Two produce duplicate reports, which is noise, not damage. |
| `skcapstone.service` | The agent daemon. Already the normal shape across the fleet; it is not the spec writer. |
| `skcomms-access.service`, `skchat-app-web.service`, `skchat-daemon.service` | Already enabled on both boxes today and have been for months. Proven parallel-safe by operation. |

The rule underneath the table, worth carrying out of this document: **a unit
is parallel-safe when its writes are scoped to the node it runs on, and
strict-handoff when its writes or its identity are fleet-scoped.** `sknoded`
is safe because the store makes it safe. `skoperator` is not, because nothing
makes it safe.

## Findings this collection turned up

Recorded, not acted on. This card is read-only.

1. **`skgateway.service` is enabled and active on `.41`** in violation of
   `builder-standby`'s `units.mustNot`. Also violated: `skcapstone-dashboard.service`,
   `skoperator.timer` and `skos-web.service` are listed in that same `mustNot`
   but have no unit file on `.41` at all, so those three are clean in fact
   even though `skfleet node doctor` grades them from a locally-collected
   inventory and can mislead (see finding 3).
2. **`skmeter.service` is enabled on `.41` and is not in
   `node-41-user-units.json`.** Live enabled count is 33, the recorded
   inventory is 32, and the difference is exactly `skmeter.service`. It is the
   per-node GPU energy counter and was enabled after the 2026-08-14
   collection. The inventory file wants regenerating; this is drift in the
   record, not on the box.
3. **`skfleet node doctor <name>` grades the LOCAL inventory against the named
   node's profile.** Running `skfleet node doctor node-41` on `.158` collects
   `.158`'s units and diffs them against `builder-standby`, which produces a
   confident, wrong report. The correct read-only check is
   `skfleet node doctor --all`, which reads published inventories. Doing that
   today prints `skipped node-41: has published no inventory yet`, so **there
   is no working automated drift check for `.41` right now.** This is a real
   precondition gap for the promotion drill and the runbook treats it as one.

## Proof nothing was changed

Every command used against `.41` was `systemctl --user list-unit-files`,
`systemctl --user cat`, `systemctl --user is-enabled` or `systemctl --user
is-active`, all read-only, plus `ls` and `pip list`. `.41`'s enabled user-unit
count was **33** at the start of this collection and **33** at the end, with
the same 33 names. Against `.158`, the same read-only verbs plus
`skfleet node doctor` and `skfleet control-bus audit`, both of which document
themselves as report-only and write nothing.
