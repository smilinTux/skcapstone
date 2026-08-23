# Replica verification: is .41 really a warm full replica?

**Epic:** `3bbf39ea`. **Card:** `6bcf1e4c`.
**Measured:** 2026-08-16, 01:38 to 01:45 EDT, against live state on both nodes.
**Method:** strictly read-only. Nothing was written to `~/.skcapstone` on either
machine; the closing check at the bottom re-counts both trees to prove it.

## Why this document exists

[adr-node-role-model.md](adr-node-role-model.md) records that .158 being the
only control seat is a deliberate, accepted single point of failure, and that
the mitigation is "a warm replica plus a drilled promotion runbook rather than
hot duplicates". .41 is that replica. Its profile declares
`stateTier: full-replica`, and the entire SPOF acceptance rests on the word
*full* being true in practice rather than in the manifest.

An accepted risk is only the risk that was actually accepted if the mitigation
is real. This card exists to convert `stateTier: full-replica` from a
declaration into a measurement. The question is deliberately not "are the two
trees identical", because they are not and are not supposed to be. The question
is: **if .158 vanished right now, what would be lost, and what would a promoted
.41 be missing?**

## Verdict

**The replica is adequate for its stated purpose. Every class of sovereign
source-of-truth state is present on .41 and is byte-for-byte identical to
.158.** All 18 content hashes match. Not one file in the 96,627-file gap
between the two trees is sovereign state; every one of them falls into a class
that `~/.skcapstone/.stignore` excludes on purpose.

The verdict comes with one material caveat and two operational findings, all
below. The caveat is that **private key material is excluded from sync by
design**, so a promoted .41 would inherit every memory, every card and every
soul, and would inherit none of the 25 secrets that let a control seat sign
things. That is a correct security decision with an incorrect operational
consequence if nobody has planned for it, and the promotion runbook
(card `591d2b1a`) is the place where it has to be planned for.

## The size gap, and why it is not the story

| | .158 (`noroc2027`, control) | .41 (`cbrd21-laptop12thgenintelcore`, builder-standby) |
|---|---|---|
| total size | **18G** | **6.6G** |
| total files | **198,834** | **160,950** |
| `agents/` | 12G | 3.3G |

```bash
# run identically on both (on .41 via: ssh cbrd21@100.86.156.5)
du -sh ~/.skcapstone
find ~/.skcapstone -type f | wc -l
du -sh ~/.skcapstone/*/ | sort -rh
```

An 11.4G gap looks alarming until you decompose it, and the decomposition is
the actual finding. Comparing full relative path lists rather than sizes:

```bash
cd ~/.skcapstone && find . -type f -printf '%P\n' | LC_ALL=C sort > /tmp/l158.txt
ssh cbrd21@100.86.156.5 'cd ~/.skcapstone && find . -type f -printf "%P\n" | LC_ALL=C sort' > /tmp/l41.txt
LC_ALL=C comm -23 /tmp/l158.txt /tmp/l41.txt | wc -l   # only on .158
LC_ALL=C comm -13 /tmp/l158.txt /tmp/l41.txt | wc -l   # only on .41
LC_ALL=C comm -12 /tmp/l158.txt /tmp/l41.txt | wc -l   # on both
```

| | files | bytes |
|---|---|---|
| only on .158 | 96,627 | 14,747,126,736 (13.73 GiB) |
| only on .41 | 58,743 | 2,559,406,053 (2.38 GiB) |
| on both | 102,207 | approx 4.2 GiB |

`LC_ALL=C` matters. The first run of this comparison used the default locale on
each side, the two `sort` orders disagreed, and `comm` silently reported 94
common files out of 198,834. A collation mismatch does not error, it just
produces a confident wrong answer, so pin the locale on both ends of any
cross-host `comm`.

The shared payload is about 4.2 GiB and it is the sovereign state. The 13.73
GiB that exists only on .158 is dominated by one thing:

| class only on .158 | files | bytes | ignore rule that excludes it |
|---|---|---|---|
| `backups/`, `agents/*/backups/`, `secure/backups/` | 103 | 14,568,444,785 (13.57 GiB) | `backups` |
| `agents/*/prompt_versions/`, `prompt_versions/` | 3,249 | 89,272,199 | `prompt_versions` |
| `agents/*/memory/archive/` | 50,547 | 65,340,451 | `**/memory/archive` |
| `pubsub/` | 25,894 | 14,057,144 | `pubsub` |
| `agents/*/skcomms/acks/` | 16,301 | 3,537,100 | `**/skcomms/acks` |
| `conversations/`, `skcode/sessions/` | 7 | 936,002 | `conversations`, `sessions` |
| `sync/outbox/Lumina-cbrd21-laptop...` | 10 | 151,086 | explicit path rule |
| **key material** (`private.asc`, `*.pem`, `*.key`) | **25** | **88,201** | `*.key`, `*.pem`, `**/private.*` |
| `metrics/daily/`, `agents/*/metrics/daily/` | 122 | 73,165 | `metrics/daily` |
| `file-transfer/` | 70 | 26,306 | `file-transfer` |
| `coordination/itil/_legacy/` | 20 | 11,976 | `(?d)coordination/itil/_legacy` |

Remaining small classes, each also covered by a rule: `**/memory/chroma`,
`**/memory/chroma-state.json`, `**/memory/index.db{,-shm,-wal}`, `__pycache__`,
`(?d)**/*.tmp` (the `.syncthing.*.tmp` staging files), `daemon.pid`,
`**/skwhisper/state.json`, `**/daemon.log`, `**/comms/outbox`,
`**/comms/archive`, `skcomms/cot-pki/{devices,packages}`, `**/*.db-{shm,wal}`,
`souls`, `connectors`.

Bytes were taken by feeding the diff list back through `stat`, so every number
above is of the real files:

```bash
cd ~/.skcapstone
grep -E '^backups/|agents/[^/]*/backups/|^secure/backups/' /tmp/only158.txt \
  | tr '\n' '\0' | xargs -0 -r stat -c '%s' | awk '{s+=$1} END{print s}'
```

So 13.57 GiB of the 13.73 GiB delta is **backup tarballs**, of which 8.9G is
`agents/lumina/backups` alone and 4.7G is the top-level `backups/`. A backup is
by construction a second copy of state that syncs anyway. The replica not
carrying the fleet's backup archive is the transport working as designed, not
a hole in the replica. Strip backups out and the entire remaining difference
between an 18G control node and a 6.6G replica is 160 MiB of caches, archives,
transient bus traffic and 86 KiB of secrets.

The `agents/` figure deserves the same treatment, because 12G versus 3.3G is
the number most likely to be quoted out of context. `agents/lumina/backups` is
8.9G of that 12G. What remains, roughly 3.1G, is what .41 holds.

One measurement artifact worth recording so nobody re-derives it: `sessions`
appears in `.stignore` and in the disposition notes as a large excluded class,
but it does not appear in the path diff at all. That is because
`~/.skcapstone/agents/*/sessions` is a **symlink** to the runtime session store
and `find` does not follow symlinks by default. `agent` is likewise a symlink to
`agents`. Neither affects the verdict, but a census that followed symlinks
would double-count and a reader comparing this doc to `du` output needs to know
why the two disagree.

## The classes that actually matter, compared by content

Counts and byte totals can coincide. Hashes cannot, so every sovereign class
was hashed as a set: each file's md5, sorted by path under `LC_ALL=C`, then
hashed again.

```bash
cd ~/.skcapstone
find agents/*/memory/short-term -name '*.json' -type f -print0 \
  | LC_ALL=C sort -z | xargs -0 -r md5sum | md5sum
```

| class | .158 | .41 | match |
|---|---|---|---|
| `agents/*/memory/short-term/*.json` | `9a7f954ca729bb1dac80cbaafc40b1db` | same | yes |
| `agents/*/memory/mid-term/*.json` | `e43007feeea7e6bf531e0fe8ffe257e4` | same | yes |
| `agents/*/memory/long-term/*.json` | `9dc71d51c9049b3eb76d705f02783c8c` | same | yes |
| `agents/*/soul` | `3cd5dc607b7e267837d4eb335e921874` | same | yes |
| `agents/*/seeds` | `314f89b5e12b5e2353d8395f1247375a` | same | yes |
| `agents/*/journal.md` | `4746db0f875bac0830c1e47a71af854f` | same | yes |
| `agents/*/trust` (incl. `febs`) | `56c83ef2e2cfef33ed3c471d0919a28e` | same | yes |
| `agents/*/memory/songs` | `79e265b7915824e475c192206da97e85` | same | yes |
| `coordination/tasks` | `4ead76785a2b3cc0d6991895559e2d25` | same | yes |
| `coordination/agents` | `f6a20852a376b85bd0a1f8d3613eb042` | same | yes |
| `coordination/card_events` | `81167d23ba5a7b408b8e32d0b634854a` | same | yes |
| `cards` | `a9275c07aaad99d9ec9caf3cc66461b3` | same | yes |
| `agents/*/comms/inbox` | `f6535b1a5eab66f1de94e3762dcb0176` | same | yes |
| `identity` | `af9a4cf10c480a4ea787fcb81c6b65ae` | same | yes |
| `config` | `3a14664eba75af8c6c0329cb15594440` | same | yes |
| `peers` | `5a48ecc7af12f1132a5dfefb193fd615` | same | yes |
| `cmdb` | `1b23d277056c793cd54095337c619fd3` | same | yes |
| `alignment` | `b8ec9452798e03cf75fc80fb2cb22f3d` | same | yes |

Eighteen for eighteen. The counts and newest-mtime census that preceded it
agrees, and is worth keeping because it says something the hashes do not: how
*fresh* each class is.

| class | files | bytes | newest mtime (identical on both) |
|---|---|---|---|
| `memory/short-term` | 13,163 | 24,355,829 | 2026-08-16 01:38 |
| `memory/mid-term` | 2,095 | 5,323,669 | 2026-08-16 01:38 |
| `memory/long-term` | 525 | 1,377,526 | 2026-08-16 01:38 |
| `soul/` | 48 | 83,958 | 2026-05-09 13:56 |
| `seeds/` | 42 | 87,347 | 2026-03-14 03:57 |
| `journal.md` | 11 | 13,130,075 | 2026-08-15 18:57 |
| `trust/febs` | 40 | 85,018 | 2026-04-25 17:16 |
| `trust/` (all) | 64 | 115,699 | 2026-08-16 01:31 |
| `coordination/tasks` | 4,805 | 5,431,890 | 2026-08-15 14:59 |
| `coordination/card_events` | 1 | 675,523 | 2026-08-15 15:10 |
| `coordination/agents` | 112 | 70,040 | 2026-08-15 15:10 |
| `agents/*/comms/inbox` | 1,717 | 3,050,861 | 2026-08-15 03:26 |
| `cards/` | 11,081 | 6,317,099 | 2026-08-16 00:00 |
| `fleet/` (store) | 82 | 86,257 | 2026-08-16 01:39 |
| `memory/songs` | 27 | 53,848,805 | 2026-04-27 02:48 |

The memory tiers were current to within seven minutes of the measurement on
both machines. `agents/*/memory/{short,mid,long}-term` counts break down per
agent identically on both sides (lumina 11,120 / 1,737 / 432; architect
1,465 / 343 / 0; jarvis 361 / 3 / 27; opus 214 / 12 / 37; plus ava, coder,
deming, grok, scholar, steward). The `soul` and `seeds` classes have old
newest-mtimes because souls and seeds are genuinely not written often, which is
the correct signature for that class rather than a staleness signal.

Two classes differ in count and both are explained. `coordination/` as a whole
is 6,142 files on .158 against 6,124 on .41, and the 18-file gap is exactly the
`coordination/itil/_legacy/` tree that `.stignore` excludes; `coordination/tasks`,
`card_events` and `agents` all hash identically. `capauth/` is 276 against 241,
and the 35-file gap is `capauth/security/tokens` (permanently excluded per the
2026-08-15 containment note), `capauth/backups/*`, and the key material.

## What would actually be lost if .158 vanished right now

Nothing in the sovereign classes. Concretely, in the order that matters:

**1. The 25 secrets, 88,201 bytes. This is the whole answer.**

Private key material never syncs, by three rules at the top of `.stignore`
(`*.key`, `*.pem`, `**/private.*`) that exist so that compromising one node
does not compromise the fleet's signing authority. The consequence is that a
promoted .41 inherits all the state and none of the authority:

- 8 swarm agent identities: `agents/{architect,artisan,ava,coder,herald,scholar,sentinel,steward}/capauth/identity/private.asc`, about 7.4 KB each.
- `capauth/service/oidc_signing_key.pem` (1,704 B), the SSO signing key.
- The entire CoT/ATAK PKI: `skcomms/cot-pki/{ca,server,ts-cot}.key` plus five device keys and their certs.
- `capauth/identity-preflight-backup-20260814T071857Z/private.asc`, a leftover of the 2026-08-14 key quarantine.

The operator key itself is **not** in this tree. `~/.skcapstone/capauth/identity/`
on .158 holds `public.asc`, `profile.json`, `custody.json` and
`root-revocation.asc` and no private key, so operator custody lives outside
`~/.skcapstone` and is not a Syncthing question at all. .41 has its own
`capauth/identity/private.asc`, so the replica can authenticate as itself; it
simply cannot sign as any of the eight swarm agents or as the OIDC issuer.

This is the correct security posture and the wrong thing to discover during an
outage. It belongs in the promotion runbook (card `591d2b1a`) as an explicit
step with a named source for each secret, because "promote .41" without it
produces a node that holds every memory and cannot issue a token.

**2. 13.57 GiB of backup archives.** Restorable only in the sense that what
they back up already synced. Losing the archive loses point-in-time rollback,
not current state: 8.9G `agents/lumina/backups`, 4.7G `backups/`, and 14 daily
GPG anchor tarballs in `secure/backups/anchors/` (2026-08-02 through 08-15).
The anchors themselves (`agents/*/anchor.json`) do sync, so only the sealed
historical snapshots go.

**3. 50,547 aged-out memory records, 62 MiB.** `**/memory/archive` is excluded
with the comment "each host runs its own cleanup; syncing archives causes data
loss", and that is a defensible call, but it makes the archive genuinely
per-node and therefore genuinely lossy. .41 holds 11,648 archived records that
.158 does not, in the other direction. These are memories already deduplicated
or aged out of the live tiers, so nothing in the working set is affected. It is
still the one non-backup class where real content exists on exactly one machine.

**4. 3,249 prompt version snapshots, 85 MiB**, and **25,894 pubsub messages,
13 MiB** (a transient bus), and **16,301 delivery acks, 3.4 MiB**, and 122
daily metric rollups, 70 file-transfer completion records, 20 `_legacy` ITIL
incident files, and one `conversations/QueenLumina.json`. History and telemetry.
Losing them costs audit depth, not capability.

**5. Ten `sync/outbox/Lumina-cbrd21-laptop12thgenintelcore-*.seed.json.gpg`
files, 148 KiB**, excluded by a rule whose stated reason is no longer true. See
the findings below.

Inverting the question: a promoted .41 would be missing the ability to sign as
eight agents and as the OIDC issuer, the CoT PKI, the backup archive, and .158's
half of the memory archive. It would be missing nothing in `agents/*/memory`,
`soul`, `seeds`, `journal.md`, `trust`, `coordination`, `cards`, `comms/inbox`,
`fleet`, `identity`, `config`, `peers`, `cmdb` or `alignment`.

## The recency bound

How far behind can .41 be in the worst case? Syncthing is eventually consistent
and .41 is a laptop, so the honest answer has a structural part and a measured
part.

**Structural ceiling: unbounded.** Nothing in the design guarantees a maximum
lag. If .41 is asleep, powered off, or off the network, it is exactly as far
behind as the length of that interruption. The folder is `sendreceive` with
`rescanIntervalS=3600` and `fsWatcherEnabled=true` on both sides, so on a live
connection propagation is filesystem-event driven and effectively seconds, with
an hourly full rescan as the backstop. That means the floor on detection after
a reconnect is fast, but the ceiling is set by the outage, not by Syncthing.

**Measured now: zero.** At 01:44 EDT on 2026-08-16 the newest files under
`~/.skcapstone` on both nodes carried the same 01:44 timestamps
(`fleet/status/node-noroc2027/node.json`, `scheduler/noroc2027/state.json`,
`skmeter/*-state.json`, `logs/cron-ledger.jsonl`). Both Syncthing index
databases were being written at 01:41 to 01:42. .41 had 629 files modified in
the previous hour, 2,692 in six hours, 5,170 in 24 hours; .158 had 553, 1,648
and 8,231 over the same windows. The replica is not merely present, it is
actively tracking.

```bash
systemctl --user is-active syncthing            # active on both
find ~/.skcapstone -type f -mmin -60 | wc -l    # .158: 553
ssh cbrd21@100.86.156.5 'cd ~/.skcapstone && find . -type f -mmin -60 | wc -l'   # .41: 629
```

**Measured worst case over the recent past: about 5.5 hours.** Two independent
sources bound it.

- Link stability. From .41's journal, the peer connection to CIHSBZ4 (.158)
  dropped twice in seven days, on 2026-08-09 at 21:56:58 and 2026-08-11 at
  21:47:00, and re-established within **one second** each time. It has been
  continuously up since 2026-08-11T21:47:01, over four days. It is a LAN
  TCP connection over IPv6, not a relay.
- Sleep. Despite the ADR's premise that .41 sleeps, it has not suspended since
  before the 2026-08-07 09:51 reboot, and has 8 days 15 hours uptime. The last
  suspend cycles in the 30-day journal were on 2026-08-02, and the longest was
  **5 hours 22 minutes** (suspend entry 10:18:38, exit 15:40:43).

So: **on current evidence the replica is current to the minute, the worst
observed lag in 30 days is about 5.5 hours, and the design admits no upper
bound at all.** For a promotion drill, plan on losing up to a working day of
writes and verify freshness at promotion time rather than assuming it. The
cheapest freshness probe is the one used above: compare the newest mtime under
`fleet/status/` on both nodes, which every node's own heartbeat keeps within a
minute of live.

```bash
ssh cbrd21@100.86.156.5 'journalctl --user -u syncthing --since "-7 days" -o short-iso \
  | grep -E "device=CIHSBZ4" | grep -iE "Established|Lost device"'
ssh cbrd21@100.86.156.5 'journalctl --since "-30 days" -o short-iso \
  | grep -iE "PM: suspend (entry|exit)"'
```

## Sync conflicts

Conflicts are the observable signature of two nodes writing the same file, so
they bear directly on whether the replica can be trusted.

```bash
find ~/.skcapstone -name '*.sync-conflict-*' | wc -l
find ~/.skcapstone -name '*.sync-conflict-*' -not -path '*/.stversions/*' | wc -l
```

| | .158 | .41 |
|---|---|---|
| all conflict files | 361 | 244 |
| excluding `.stversions/` | 361 | 216 |
| touching a source-of-truth class | **13** | **13** |

The 13 are identical on both sides and all predate August:

- `agents/lumina/journal.sync-conflict-20260723-{075909,215118}-CIHSBZ4.md` (2)
- `agents/opus/trust/trust.sync-conflict-20260710-*.json` (10)
- `agents/lumina/trust/trust.sync-conflict-20260710-074312-CIHSBZ4.json` (1)

By conflict timestamp, 317 of the 361 are from 2026-07 and 44 from 2026-08. The
August ones are concentrated in `agents/*/logs/skmem-reconcile.*.log`, which is
a log file two hosts each append to, and is noise rather than divergence. There
are **no conflicts at all** in `memory/{short,mid,long}-term`, `soul`, `seeds`,
`cards` or `coordination/tasks`, which is the result that matters: the classes a
promotion depends on are not being concurrently written.

The remaining .158-side excess (361 against 216) sits under
`agents/lumina/` (216 against 62) in the archive and log subtrees that do not
sync, so it is the same ignore-rule story as the size gap.

The two `trust.json` clusters are worth a follow-up but not an alarm: both
device suffixes appear (`CIHSBZ4` = .158, `4U3J4V6` = .41), which means .41 was
writing its own `trust.json` on 2026-07-10. That is a single-writer question for
the trust file specifically, it stopped over a month ago, and the live
`trust/` class hashes identically today.

## Two findings that are not the verdict but should not be lost

**Finding 1: the two nodes do not agree on who else receives this folder.**

.158's `skcapstone-sync` block shares to CIHSBZ4 (itself), S5G63MA
(`ollama-gpu`, which is .100) and 4U3J4V6 (.41). .41's block shares to CIHSBZ4,
4U3J4V6 (itself) and **YXFYBLT (`sksync.skstack01.douno.it`)**, a remote host
that .158 does not list, and does not list S5G63MA.

```bash
sed -n '/folder id="skcapstone-sync"/,/<\/folder>/p' ~/.local/state/syncthing/config.xml
ssh cbrd21@100.86.156.5 'sed -n "/folder id=\"skcapstone-sync\"/,/<\/folder>/p" ~/.config/syncthing/config.xml'
```

Two things follow. The presence of `ollama-gpu` in .158's list contradicts the
ADR's `worker-gpu` row, which declares `stateTier: none` and
`syncFolders: [skfleet-control]`; the profile says .100 should not be on this
folder and the transport still offers it. And .41 offering the sovereign folder
to an off-fleet host is a state-tier decision made in a config file rather than
in a manifest, which is precisely the failure mode the role model was written to
end. Neither is this card's to fix. Both are drift between
`deploy/fleet-objects/profile/` and the running transport, and belong with the
scoped-folder work in [control-bus-folder.md](control-bus-folder.md).

Note also a version skew: .158 runs Syncthing v1.27.2-ds4 with the LevelDB index
(`~/.local/state/syncthing/index-v0.14.0.db`) while .41 runs a 2.x with the
SQLite index (`~/.config/syncthing/index-v2/`). The config paths differ too,
which is why a single path works on neither node. They interoperate correctly
today; the skew is worth knowing before anyone upgrades one side.

**Finding 2: an ignore rule rests on a false premise.**

`.stignore` carries this rule, under a comment block that calls
`cbrd21-laptop12thgenintelcore` a decommissioned node no longer in the mesh, and
justifies the rule with "Its seed files can never be reconciled (device gone),
ignore permanently":

```
sync/outbox/Lumina-cbrd21-laptop12thgenintelcore-*
```

`cbrd21-laptop12thgenintelcore` is .41. It is device 4U3J4V6, it is in the mesh,
it is the subject of this document, and it was reachable throughout the
measurement. The rule permanently excludes ten seed files, 148 KiB, on the
grounds that the device is gone. The rule may still be worth keeping, since a
seed outbox is per-host operational state like the others in that section, but
the stated reason is wrong and a future reader will trust it. It should be
re-derived and the comment corrected or the rule dropped.

**Also observed, benign:** .158's Syncthing repeats
`Puller ... item "agents/jarvis/heartbeats/jarvis.json" ... pull: no such file`
and `isn't making sync progress - retrying in 1m0s` several times a day. The
target is a heartbeat file rewritten every few seconds, so the puller is losing
a race against the writer, not blocking. It resolves on the next pass. It does
mean the log line "isn't making sync progress" is not a reliable staleness
signal on this fleet, which is why this document uses mtimes instead.

## Read-only confirmation

Both trees were re-counted after all measurement, using the same commands as at
the top:

| | .158 files | .158 size | .41 files | .41 size |
|---|---|---|---|---|
| before (01:38) | 198,827 | 18G | 160,942 | 6.6G |
| after (02:05) | 199,024 | 18G | 160,997 | 6.6G |

Sizes are unchanged. The counts moved by +197 and +55 over the 27 minutes of
measurement, and that drift is live daemon activity rather than this
investigation. Re-running the path census at the end and diffing it against the
opening one places every new path in a daemon-written class:

```bash
LC_ALL=C comm -13 /tmp/l158.txt /tmp/l158-after.txt | cut -d/ -f1 | sort | uniq -c | sort -rn
```

```
182 pubsub      54 agents (comms/inbox deliveries, sync/outbox seeds)
 11 skcomms      6 cards       2 sync      2 scheduler      1 coordination
```

66 `pubsub/` paths also disappeared in the same window, which is a message bus
draining. Nothing appeared outside those classes.

Every command in this document is `find`, `du`, `stat`, `md5sum`, `grep`,
`sed`, `ls`, `wc`, `comm`, `journalctl`, `date` or `systemctl is-active`. None
of them creates, modifies or deletes a path under `~/.skcapstone` on either
host. Scratch files went to the session scratchpad and `/tmp` only.

## Reproducing this

```bash
# 1. size and file census, both hosts
du -sh ~/.skcapstone; find ~/.skcapstone -type f | wc -l; du -sh ~/.skcapstone/*/ | sort -rh

# 2. path diff (pin LC_ALL=C on BOTH sides)
cd ~/.skcapstone && find . -type f -printf '%P\n' | LC_ALL=C sort > /tmp/l158.txt
ssh cbrd21@100.86.156.5 'cd ~/.skcapstone && find . -type f -printf "%P\n" | LC_ALL=C sort' > /tmp/l41.txt
LC_ALL=C comm -23 /tmp/l158.txt /tmp/l41.txt > /tmp/only158.txt
cut -d/ -f1 /tmp/only158.txt | sort | uniq -c | sort -rn

# 3. every delta class must map to a rule in ~/.skcapstone/.stignore
cat ~/.skcapstone/.stignore

# 4. content hashes of the sovereign classes (the load-bearing step)
find agents/*/memory/short-term -name '*.json' -type f -print0 \
  | LC_ALL=C sort -z | xargs -0 -r md5sum | md5sum

# 5. conflicts
find ~/.skcapstone -name '*.sync-conflict-*' -not -path '*/.stversions/*' | wc -l

# 6. freshness
find ~/.skcapstone -type f -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort -r | head
```

## Follow-ups this card surfaced

1. **Promotion runbook must enumerate the 25 secrets** and where each is
   recovered from. Card `591d2b1a`. Without it, promotion produces a node that
   holds all the state and can sign nothing.
2. **Transport drift against the profile manifests**: `.100` still shared into
   `skcapstone-sync` from .158; `sksync.skstack01.douno.it` shared into it from
   .41. Belongs with [control-bus-folder.md](control-bus-folder.md).
3. **Correct or remove the stale `.stignore` comment** claiming
   `cbrd21-laptop12thgenintelcore` is decommissioned.
4. **Consider whether `**/memory/archive` should stay per-node.** It is the only
   non-backup class where content exists on exactly one machine, 50,547 records
   on .158 and 11,648 on .41.
5. **Schedule this verification.** A replica verified once is a replica verified
   once. Section "Reproducing this" is deliberately a script's worth of commands.
