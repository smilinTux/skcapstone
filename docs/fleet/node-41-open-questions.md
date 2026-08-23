# .41 open questions: `skchat-daemon-jarvis` and `cloudflared-fed`

Card `67e8c15f`, epic `3bbf39ea`. Two units on `node-41` could not be given a
disposition without a decision only Chef can make. This document does not make
the decision. It gathers the evidence so that making it is quick, and it spells
out what breaks under each answer so that neither option is a guess.

`.41` is `ssh cbrd21@100.86.156.5` (Tailscale). The old `192.168.0.41` is dead.
The comparison node called `.158` in the epic is `noroc2027`, which holds
`192.168.0.158`; the control-node inventory is filed under `node-noroc2027`.

Everything below was collected with read-only verbs. Nothing on either node was
started, stopped, enabled, disabled, reloaded or edited. The confirmation that
`.41` is unchanged is at the end.

Evidence timestamp: 2026-08-16, roughly 12:35 EDT.

---

## Q1. Is `skchat-daemon-jarvis.service` load-bearing, or a leftover?

### The short version of what the evidence says

It is not a leftover, and it is not a duplicate of the other daemon either.
The surprise is which of the two daemons is idle. `.41` runs two enabled skchat
daemons, and the one named after `jarvis` is the one carrying all of the
traffic. The unnamed one, `skchat-daemon.service`, is not lumina on this box.
It runs as `opus`, and it has received nothing at all.

So the "half-alive duplicate" pattern is present, but inverted from the way the
unit names suggest. Deleting the oddly-named unit would delete the working one.

### Is it running, and for how long

```
systemctl --user show skchat-daemon-jarvis.service \
  -p ActiveState -p SubState -p UnitFileState -p MainPID \
  -p ExecMainStartTimestamp -p NRestarts -p Result -p CPUUsageNSec
```

| | `skchat-daemon-jarvis.service` | `skchat-daemon.service` |
|---|---|---|
| `ActiveState` | `active (running)` | `active (running)` |
| `UnitFileState` | `enabled` | `enabled` |
| `MainPID` | 5183 | 2471605 |
| started | 2026-08-07 09:51:55 EDT | 2026-08-13 01:33:46 EDT |
| `NRestarts` | 0 | 0 |
| `Result` | `success` | `success` |
| CPU consumed | 64,016 s | 27,864 s |

Both have been up continuously with zero restarts. Neither is in a crash loop,
so the restart-cycle hypothesis is ruled out.

The CPU numbers deserve interpretation rather than just recording. 64,016
seconds of CPU across 218 hours of wall clock is about 8 percent of one core
held continuously, and the opus daemon burns about 9 percent by the same
arithmetic. Together the two daemons hold roughly a fifth of a core forever,
purely to poll. `.41` was showing a load average of 6.57 at collection time on a
box whose whole job in the role model is `builder-standby`, so this is a real
share of a machine that is supposed to be mostly idle. That cost is per daemon,
which means it is one of the concrete things a disposition decision buys back.

### Do they hold listening sockets, and does anything connect

```
ss -ltnp
ss -tnp state established | grep -E '938[0-9]'
```

| daemon | socket | binding | peers connected |
|---|---|---|---|
| jarvis | `127.0.0.1:9389` | loopback only | none |
| opus | `0.0.0.0:9385` | all interfaces | none |

Both hold a health socket. Neither had a single established connection at
collection time. The jarvis health port is loopback-bound by its own
`SKCHAT_HEALTH_PORT=9389` and `SKCHAT_HEALTH_HOST=127.0.0.1` environment, so it
is not reachable off the box at all, and nothing on the box was talking to it.

That matters for the decision because it means no health checker, dashboard or
probe is currently depending on the jarvis daemon being answerable. Whatever
value it provides is not being provided through its socket.

The opus daemon is the one bound to `0.0.0.0`, via a `bind.conf` drop-in that
sets `SKCHAT_HEALTH_HOST=0.0.0.0`. That is the wider exposure of the two, and it
belongs to the idle daemon.

### Does the journal show real work, or an idle loop

```
journalctl --user -u skchat-daemon-jarvis.service --no-pager -o short-iso \
  | grep -v 'No new messages'
```

The jarvis daemon logs to the journal and the overwhelming majority of its
lines are `No new messages (polls: 143052, uptime: 218h 42m)`. But it is not
purely idle. It has received messages, and it is the only one of the two that
has:

```
cat ~/.skchat-jarvis/daemon_stats.json
cat ~/.skchat/daemon_stats.json
```

| | jarvis | opus |
|---|---|---|
| `messages_received` | 155 | 0 |
| `messages_sent` | 0 | 0 |
| `uptime_seconds` | 787,420 | 298,903 |
| `transport_status` | healthy | healthy |
| `webrtc_signaling_ok` | false | false |
| `webrtc_signaling_health` | degraded | degraded |
| `online_peer_count` | 1 | 1 |

The opus daemon has received zero messages in three and a half days of uptime.
If the question were asked about that unit instead, the honest answer would be
that nothing has ever arrived for it.

### When did it last do something meaningful, and what was it

Most recent receipt was 2026-08-16 08:55:43 EDT, about three and a half hours
before collection. So it is not stale. But the content of the traffic changes
what "meaningful" means here:

```
grep -oE '\[chef\] .*' ~/.skchat-jarvis/daemon.log | sort | uniq -c | sort -rn
```

```
     51 [chef] standup time
     50 [chef] second
     50 [chef] now a group
```

All 151 logged message bodies are three distinct strings, each delivered about
fifty times. The receipt counter climbs in steps of three at irregular
intervals: 117, 120, 123, 126, 130, 133, 136, ... 155. These read as test
messages from a group-chat exercise that are being re-sent, not as a live
conversation.

The archive tells us this is a sender-side replay rather than a receiver failing
to acknowledge. Each redelivery lands as new envelope files with fresh UUIDs:

```
ls ~/.skcapstone/agents/jarvis/comms/{inbox,archive,outbox} | wc -l
```

| directory | files |
|---|---|
| `inbox` | 0 |
| `archive` | 72 |
| `outbox` | 78 |

The inbox is drained cleanly, which means the daemon is doing its job correctly.
Something upstream keeps re-emitting the same three test messages. The 78 files
sitting in `outbox` against a `messages_sent` count of zero is a separate
smell, and it is the same shape as the outbox accumulation recorded in the seed
outbox flood incident.

So the accurate characterisation is: the jarvis daemon works, and it is being
fed a stuck test payload. It is doing real work on unreal input.

### Is the jarvis identity real, and is it distinct from the other daemon

This is a genuine question because the record says the `chi` cluster had no
jarvis PGP key and its profile named a key that did not exist locally, later
resolved to Casey's `C8D406A4`. On `.41`, which is Chef's `nor` cluster and a
separate sovereign install, the answer is different and clean.

```
gpg --list-secret-keys --keyid-format LONG
cat ~/.skcapstone/agents/jarvis/capauth/identity/profile.json
cat ~/.skcapstone/agents/jarvis/identity/identity.json
```

| source | fingerprint |
|---|---|
| local secret keyring, uid `Jarvis (SK Sovereign Agent) <jarvis@skworld.io>` | `BCF7ED87AC8117B448B7677F45BF78F335767EF8` |
| `capauth/identity/profile.json` `key_info.fingerprint` | `BCF7ED87AC8117B448B7677F45BF78F335767EF8` |
| `identity/identity.json` `pgp_fingerprint` | `BCF7ED87AC8117B448B7677F45BF78F335767EF8` |

All three agree, the private half is present on the box, the key is rsa4096
valid to 2028-03-01, and it is not `C8D406A4`. The profile also carries an
advocate delegation to Chef (`BD7EEECA23D90A594400751CFDB582D9CB7272A6`) with
capabilities including `send`, `sign`, `coordinate`, `ops`. The private key
material lives at `~/.skcapstone/agents/jarvis/capauth/identity/private.asc`;
only the path and the field name are recorded here, never a value.

The opus identity on the same box is a different key
(`985FADA515343091`, uid `Opus (SK Sovereign Agent) <opus@skworld.io>`). The two
daemons are genuinely two identities, not one identity started twice.

### Do the two daemons overlap or divide work

They divide storage cleanly and overlap on behaviour.

They divide: separate `SKCHAT_HOME` (`~/.skchat-jarvis` versus `~/.skchat`),
separate health ports (9389 versus 9385), separate agent trees, separate keys.
There is no shared state for them to corrupt.

They overlap: both run the same `skchat daemon start --interval 5` loop, and
both file logs show the identical background behaviour:

```
tail ~/.skchat-jarvis/daemon.log
grep -v 'No new messages' ~/.skchat/daemon.log | tail
```

Both emit, once per minute each, a heartbeat addressed to `*` as a
`SignedEnvelope` "via failover", and both attempt
`ws://127.0.0.1:9390/webrtc/ws?room=skcomms-agent&peer=agent`. The `room` is the
same for both, so on the signaling plane they are two clients presenting as the
same room member rather than two participants dividing anything.

Two independent things fall out of reading those two lines carefully.

First, nothing is listening on 9390:

```
ss -ltn | grep -c 9390   # returns 0
```

The loopback ports in use on `.41` are 9386, 9387, 9389, 9391, 9400, 9420. There
is no 9390. Both daemons have therefore been reconnecting to a dead signaling
broker once a minute for their entire uptime, which is why both report
`webrtc_signaling_health: degraded`. That is a pre-existing defect independent of
this card, and it is not caused by either daemon; it will survive whichever way
Q1 is answered.

Second, the broadcast heartbeat to `*` is the same shape as the Syncthing
outbox flood of 2026-07-23. Two daemons doubles that emission rate. Whichever
daemon is retired, the flood pressure halves.

### The `SKCOMMS_HOME` drop-in is the reason this unit looks abandoned

```
systemctl --user cat skchat-daemon-jarvis.service
```

The unit carries a `fix-comms-home.conf` drop-in dated 2026-07-22 that unsets
`SKCOMMS_HOME`. Its own comment explains that the variable had nested the jarvis
comms path to `.../jarvis/skcomms/agents/jarvis/comms`, an empty tree no sender
writes to, so the daemon polled a directory that could never receive anything.

This is the single most important piece of context for reading the history. For
some period before 2026-07-22, this daemon genuinely was doing nothing, because
it was watching the wrong directory. Any impression that it is dead probably
dates from then. It was repaired, and the message receipts from 2026-08-13
onward are the proof the repair worked. Judging it on pre-July behaviour would
be judging a bug that has been fixed.

### The fact that decides the blast radius

`skchat-daemon-jarvis.service` is not the only thing on `.41` running as jarvis.

```
systemctl --user cat skcomms.service
```

`skcomms.service`, which owns `0.0.0.0:9384` and is the backend for the public
tunnel hostnames in Q2, sets:

```
Environment=SKAGENT=jarvis
Environment=SKCAPSTONE_AGENT=jarvis
Environment=SKMEMORY_AGENT=lumina
Environment=SKCHAT_IDENTITY=capauth:opus@skworld.io
```

Three different agent names in one unit. The jarvis identity on `.41` is
therefore load-bearing for the transport daemon regardless of what happens to
the skchat daemon. Retiring the skchat daemon does not retire jarvis from this
node, and any plan that assumes it does will leave `skcomms` running under a
half-removed identity. That unit's own internal inconsistency is worth a
separate card whichever way this one goes.

### How `.158` handles the same situation

```
systemctl --user list-unit-files 'skchat*'
```

On `.158` the convention is explicit and different:

| unit | state |
|---|---|
| `skchat-daemon.service` (agent `lumina`) | enabled |
| `skchat-daemon-opus.service` | disabled |
| `skchat-daemon-chef.service` | disabled |

`.158` keeps exactly one enabled daemon, for the node's primary identity, and
keeps the per-agent daemons on disk as disabled units so they can be started on
demand without being maintained. `.41` diverges by having two enabled at once,
and by having neither of them be lumina.

That gives the decision a precedent to match rather than a judgement call to
invent.

### Decision A: keep `skchat-daemon-jarvis.service`

What you are keeping is the only daemon on `.41` that receives anything, backed
by a real and self-consistent PGP identity, aligned with the `skcomms` unit that
already runs as jarvis.

What breaks or persists if you pick this:

- The 8 percent of a core, the 30 MB and growing `daemon.log`, and the
  per-minute broadcast heartbeat all continue, on a node whose role is
  `builder-standby`.
- `.41` stays divergent from the `.158` one-enabled-daemon convention, so the
  install profile work for the `builder-standby` role has to encode `.41` as a
  special case or the profile will fight the node.
- The stuck three-message replay keeps arriving. Keeping the daemon means
  keeping the thing that makes the replay visible, which is arguably an argument
  for keeping it until the replay is traced.
- Nothing is at risk, because nothing connects to it. This option cannot cause
  an outage.

### Decision B: retire `skchat-daemon-jarvis.service`

Retire means disable, matching how `.158` holds `skchat-daemon-opus.service`, so
the unit file stays on disk and the identity stays intact.

What breaks if you pick this:

- The three replayed test messages stop being delivered anywhere. Since the
  inbox drains to archive and nothing reads the archive, no consumer loses data,
  but the replay becomes invisible and the underlying sender bug stops
  announcing itself. Trace the sender before or instead of silencing it.
- `.41` would be left with one enabled skchat daemon that has received zero
  messages in three and a half days. That is a strictly worse end state than
  `.158`, where the single enabled daemon is the node's real identity. If you
  retire jarvis you should decide in the same breath what `skchat-daemon.service`
  running as `opus` is for, because retiring the working one and keeping the
  idle one is the exact inversion this epic exists to prevent.
- Nothing else breaks. No socket consumer, no dependent unit, and no `Wants` or
  `Requires` edge points at it. `skcomms.service` keeps running as jarvis
  independently, so the jarvis identity does not disappear from the node.
- Recovered: about 8 percent of a core, a log file growing past 30 MB, and half
  the broadcast heartbeat rate.

---

## Q2. Is `cloudflared-fed.service` an intentional second ingress, or drift?

### The short version of what the evidence says

It is intentional in origin and drifted in content. `.158` runs a unit with the
same name, the same shape and the same purpose, so `.41` having one is a
deliberate pattern rather than an accident. But two of `.41`'s four public
routes point at ports that nothing is listening on, and the two routes that do
work are two hostnames aimed at the same single backend. The unit's own
description advertises the half that is dead.

Separately, and this was not expected, `.41` has a second cloudflared that no
systemd unit owns.

### What tunnel, what hostnames, what routes

```
systemctl --user cat cloudflared-fed.service
cat ~/.cloudflared/config.yml
```

The unit runs `cloudflared --no-autoupdate --config ~/.cloudflared/config.yml
tunnel run fed-skworld-41`, tunnel UUID `d17d7460-0130-467f-b057-55329af15573`.
Its credentials file is
`~/.cloudflared/d17d7460-0130-467f-b057-55329af15573.json`, mode `0400`. Only
that path is recorded; no credential value was read or reproduced.

| hostname | routes to | backend live? |
|---|---|---|
| `fed-opus.skworld.io` | `http://127.0.0.1:9384` | yes |
| `fed-jarvis.skworld.io` | `http://127.0.0.1:9384` | yes |
| `skpay-swarm-skstack41.skworld.io` | `http://127.0.0.1:18402` | no |
| `sksso-swarm-skstack41.skworld.io` | `http://127.0.0.1:19000` | no |
| catch-all | `http_status:404` | n/a |

### Is the route live right now

The tunnel process itself is healthy. It is PID 5177, started 2026-08-07
09:51:55 EDT, `NRestarts=0`, and its journal shows four registered edge
connections established on 2026-08-15 at 15:43:37 after a burst of QUIC dial
failures earlier that afternoon:

```
journalctl --user -u cloudflared-fed.service --no-pager -n 25 -o short-iso
```

The failures were DNS: `lookup protocol-v2.argotunnel.com on 100.100.100.100:53:
no such host`. That resolver is the Tailscale MagicDNS address, which ties this
to the previously recorded CoreDNS and Tailscale DNS forwarding issue rather
than to anything about the tunnel's configuration. It recovered on its own. The
only other recent line is a version warning: cloudflared 2026.7.2 against a
current 2026.8.2.

So the tunnel is connected to the Cloudflare edge right now. Whatever DNS points
at it is being served.

The backends are a different matter:

```
for p in 9384 18402 19000; do ss -ltn | grep -q ":$p " && echo "$p LISTENING" || echo "$p NOT LISTENING"; done
```

```
9384 LISTENING
18402 NOT LISTENING
19000 NOT LISTENING
```

Only 9384 answers. It is owned by `skcomms.service`, the FastAPI transport
daemon, bound `0.0.0.0:9384` by a `bind.conf` drop-in, PID 1752658.

This is the finding that reframes the whole question. The unit's `Description`
reads `cloudflared tunnel fed-skworld-41 (swarm public ingress: sksso-swarm /
skpay-swarm)`. That is the stated reason the unit exists, and both routes named
in it are dead. The half of the unit that still functions, the two `fed-*`
hostnames, is not mentioned in its description at all.

The swarm services do exist; they are simply not reachable the way the tunnel
assumes. They run in k3s:

```
sudo -n k3s kubectl get svc -A | grep -iE 'sksso|skpay'
```

| namespace | service | type | ports |
|---|---|---|---|
| `skpayment` | `skpayment` | NodePort | `8000:30802/TCP` |
| `sksso` | `sksso` | ClusterIP | `9000/TCP` |
| `sksso` | `sksso-server` | ClusterIP | `80/TCP, 443/TCP` |
| `sksso` | `skdata` | ClusterIP | `5432/TCP` |
| `sksso` | `skcache` | ClusterIP | `6379/TCP` |

`cloudflared` runs on the host and dials `127.0.0.1`, but `sksso` is ClusterIP
only and reachable solely from inside the cluster, and `skpayment` is exposed on
NodePort 30802 rather than the 18402 the config names. Nothing bridges 18402 or
19000 into the host namespace. These are not ports that went away recently; the
routing model never lined up.

### And there is a second, unmanaged cloudflared

```
ps -eo pid,etime,args | grep -i '[c]loudflared'
systemctl --user list-units 'cloudflared*' --all
systemctl list-units 'cloudflared*' --all
```

Two further cloudflared processes are running, PIDs 3825399 and 3825403, both
started 2026-08-15 15:42:56, running `--config /etc/cloudflared/config.yaml`.
No systemd unit at either user or system scope claims them. `/etc/cloudflared/`
on the host contains only `cloudflared.yml.example`, so the config path they
name does not exist on the host filesystem.

Walking the parent chain explains it:

```
ps -o pid,ppid,args -p 3825399
cat /proc/3825399/status | grep PPid
```

Their parent is a `containerd-shim-runc-v2` under `/run/k3s/containerd`. They
are pods:

```
sudo -n k3s kubectl get pods -A -o wide | grep -i cloudflare
```

```
sksso   cloudflared-skstack41-845c796979-4n949   1/1  Running  2 (20h ago)  8d
sksso   cloudflared-skstack41-845c796979-wt6dg   1/1  Running  2 (20h ago)  8d
```

So the sksso stack ships its own in-cluster cloudflared deployment, two
replicas, and that is the one that can actually reach the ClusterIP services the
systemd unit is failing to reach. The systemd `cloudflared-fed.service` and the
in-cluster `cloudflared-skstack41` overlap in stated purpose, and only the
in-cluster one is positioned to fulfil it.

That gives `.41` three public ingress paths in total: the systemd tunnel, and
two pod replicas of a second tunnel. Only one of them is visible to any
systemd-based inventory, which is why the fleet unit inventory did not catch it.

### Does `.158` run an equivalent

Yes, and the comparison is the cleanest evidence that the unit is intentional.

```
systemctl --user cat cloudflared-fed.service
cat ~/.cloudflared/config.yml
```

| | `.41` | `.158` |
|---|---|---|
| unit name | `cloudflared-fed.service` | `cloudflared-fed.service` |
| tunnel name | `fed-skworld-41` | `fed-skworld` |
| tunnel UUID | `d17d7460-...` | `5b5e2bd2-...` |
| hostnames | 4 | 1 |
| fed hostname | `fed-opus`, `fed-jarvis` | `fed-lumina` |
| backend | `127.0.0.1:9384` | `127.0.0.1:9384` |
| binary | `/usr/bin/cloudflared` | `/usr/local/bin/cloudflared` |
| uptime at collection | 9d 02h | 8d 14h |

`.158`'s config carries a comment stating its purpose directly: a neutral
Cloudflare-fronted ingress to the skcomms inbox, hiding the tailnet hostname
behind `fed-lumina.skworld.io`. `.41` does the same thing for its own agents.
The naming is even consistent: one `fed-<agent>.skworld.io` per agent identity
on the node. `.158` has one agent, so one hostname. `.41` has two, so two
hostnames.

That last detail links Q1 and Q2. `fed-jarvis.skworld.io` exists because `.41`
runs a jarvis agent. But it does not route to the jarvis skchat daemon, which
listens on loopback 9389. It routes to 9384, the shared `skcomms` daemon, which
is exactly where `fed-opus.skworld.io` also routes. The per-agent naming is
cosmetic; there is one backend behind both names, and it is a unit that itself
declares three different agent identities in its environment.

### The security-relevant reading

A second public ingress is a fact worth stating plainly whichever way it is
classified.

What is exposed is `skcomms` on 9384, and it is not naked. `skcomms.service`
carries a `strict-signed.conf` drop-in setting `SKCOMMS_SKFED_STRICT_SIGNED=1`,
so the federation path requires signed envelopes. The daemon binds `0.0.0.0`
rather than loopback, which means it is reachable from the LAN and the tailnet
as well as through the tunnel; the tunnel is not the only path to it. The two
dead swarm hostnames expose nothing, because `cloudflared` returns an error for
a backend that refuses connection, but they are DNS names publicly associated
with this node advertising services by name.

No probe was made from the public internet. Everything above is local config and
local process state.

### Decision A: keep `cloudflared-fed.service` as an intentional second ingress

What you are keeping is a working tunnel to `skcomms`, matching a pattern `.158`
already uses, protected by strict signed federation.

What breaks or persists if you pick this:

- `.41` keeps a second public ingress into the fleet, and any future security
  review has to account for two attack surfaces instead of one. The mitigation
  is already in place (`SKCOMMS_SKFED_STRICT_SIGNED=1`), but it is now
  load-bearing for a public path rather than an internal one, so turning it off
  later becomes a public exposure rather than a config change.
- Two of the four routes stay dead and the unit description keeps advertising
  them. Anyone reading the unit six months from now learns the wrong thing about
  what it does. If you keep the unit, the description and the two dead ingress
  entries should be corrected in the same change, or this exact investigation
  gets repeated.
- The overlap with the in-cluster `cloudflared-skstack41` deployment persists.
  Two tunnel implementations claiming the sksso stack is precisely the duplicate
  pattern this epic removes, and only the pod version works.
- `fed-jarvis.skworld.io` continues to imply a per-agent backend that does not
  exist. If Q1 is answered by retiring the jarvis daemon, this hostname becomes
  actively misleading rather than merely redundant.

### Decision B: treat it as drift and retire it

What breaks if you pick this:

- `fed-opus.skworld.io` and `fed-jarvis.skworld.io` stop resolving to anything.
  Nothing on `.41` was observed depending on them, but this is the one
  consequence that cannot be verified from inside the node. Any external client,
  federation peer or bookmark using those names would break, and the evidence
  here cannot rule that out. Check the Cloudflare DNS records and any federation
  peer configuration before disabling, because a public hostname's consumers are
  by definition off-box.
- `.41` loses the funnel-privacy property `.158`'s config comment describes: the
  tailnet hostname stops being hidden behind a neutral public name for any peer
  that reaches this node's skcomms.
- The two swarm hostnames lose nothing, because they already route to closed
  ports.
- The in-cluster `cloudflared-skstack41` pods are unaffected. They are managed by
  k3s, not systemd, so disabling the systemd unit does not touch them and `.41`
  still has a public ingress afterwards. Retiring this unit reduces `.41` from
  three ingress paths to two; it does not close the node.
- `.41` diverges from `.158`, which keeps its equivalent. If the intent is a
  uniform fleet, the matching decision on `.158` needs making at the same time,
  or the two nodes drift in the opposite direction.

---

## Confirmation that `.41` was not modified

Only read-only verbs were used against `.41`: `systemctl --user cat`,
`systemctl --user show`, `systemctl --user list-unit-files`,
`systemctl --user list-units`, `journalctl --no-pager`, `ss -ltn` and `ss -ltnp`,
`ps`, `ls`, `cat`, `grep`, `wc`, and `kubectl get`. No unit was started,
stopped, restarted, enabled, disabled or reloaded. No file was written.

The required check is that the enabled-unit count equals the inventory in
`docs/fleet/inventories/node-41-user-units.json` plus `skmeter.service`, which is
a known addition made after that inventory was taken.

```
systemctl --user list-unit-files --state=enabled --no-pager --no-legend | wc -l
```

| | count |
|---|---|
| `node-41-user-units.json` | 32 |
| plus `skmeter.service` | 33 |
| observed on `.41` | 33 |

The counts match. Comparing the sorted names as well as the totals, the observed
set is the inventory set with `skmeter.service` added and nothing else changed,
so the match is not a coincidence of two offsetting differences.

`.41` is unmodified.

## Findings outside the scope of this card

Recorded here because they were found while gathering the evidence and would
otherwise be lost. None of them should be fixed as part of card `67e8c15f`.

1. Nothing listens on `127.0.0.1:9390`, and both skchat daemons on `.41` have
   been reconnecting to that WebRTC signaling broker once a minute for their
   entire uptime. Both report `webrtc_signaling_health: degraded`. This is
   independent of either disposition decision.
2. Something is re-sending the same three test messages (`standup time`,
   `second`, `now a group`) to jarvis roughly fifty times each, as new envelopes
   with fresh UUIDs. The receiver is behaving correctly; the sender is looping.
3. `~/.skcapstone/agents/jarvis/comms/outbox` holds 78 files while
   `messages_sent` is 0. Same shape as the recorded seed outbox flood.
4. `skcomms.service` on `.41` sets `SKAGENT=jarvis`, `SKMEMORY_AGENT=lumina` and
   `SKCHAT_IDENTITY=capauth:opus@skworld.io` in one unit. Three identities in one
   service is going to confuse any identity-scoped policy applied later.
5. The in-cluster `cloudflared-skstack41` deployment in namespace `sksso` is a
   public ingress invisible to systemd-based unit inventories. The fleet
   inventory format cannot currently see workload-managed ingress at all.
6. `cloudflared` on `.41` is version 2026.7.2 against a current 2026.8.2.
