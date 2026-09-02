# .100 NFS export: the leaked heredoc, and the `no_root_squash` decision

Card `f4fc262d` (epic `3bbf39ea`). Evidence collected 2026-08-16 over
`ssh cbrd21@192.168.0.100`. **Nothing on .100 was modified by this pass.**
Only `cat`, `stat`, `ls`, `df`, `findmnt`, `ss`, `journalctl`, `exportfs -v`,
`showmount` and `grep` were run. The export list is byte-identical before and
after.

## 1. What happened

`/etc/exports` on .100 was written by a shell heredoc whose terminator was
never matched. The terminator and the three commands that were supposed to run
*after* the heredoc all got swallowed into the file instead. The pre-fix
content is preserved verbatim at `/etc/exports.bak-20260814T203233Z`:

```
/srv/comfyui 192.168.0.0/24(rw,sync,no_subtree_check,no_root_squash)
EOEXPORTS
exportfs -ra 2>/dev/null || /usr/sbin/exportfs -ra
systemctl restart nfs-kernel-server 2>/dev/null || service nfs-kernel-server restart
showmount -e
```

Only the first line was intended. `exportfs` parses each remaining line as an
export entry of the form `<path> <clients...>`. Lines 2 to 5 have no client
spec, so `exportfs` warned `No host name given with EOEXPORTS` and applied the
default, which is **export to everyone**. `EOEXPORTS`, `exportfs`,
`systemctl` and `showmount` therefore became four world-facing export paths.
They pointed at directories that do not exist, so no data was readable through
them, but they were live entries in the export table visible to any host that
could reach `rpc.mountd` on .100.

The reconstructed original command was shaped like this, and the failure mode
is that the inner terminator was consumed by an outer quoting layer (an
`ssh host '...'` wrapper or a heredoc nested inside another heredoc), so `tee`
kept reading to EOF:

```sh
sudo tee /etc/exports > /dev/null <<'EOEXPORTS'
/srv/comfyui 192.168.0.0/24(rw,sync,no_subtree_check,no_root_squash)
EOEXPORTS
exportfs -ra ...
```

## 2. Current state

`/etc/exports` now carries one export with an explicit three-host client list.
Verified `exportfs -v`, `/var/lib/nfs/etab` and `showmount -e` all agree:

| client | address | role |
|---|---|---|
| jarvis-laptop | 192.168.0.41 | builder-standby |
| ollama (this host) | 192.168.0.100 | worker-gpu, loopback |
| norap0015 | 192.168.0.155 | worker |

Proven both directions on 2026-08-14: .158 is unlisted and was refused
(`rpc.mountd: refused mount request from 192.168.0.158 for /srv/comfyui:
unmatched host`, journal 20:32:58), .41 is listed and mounted. The four bogus
world-facing entries are gone.

### The export is a symlink farm, not data

`/srv/comfyui` is 4.0K total and contains **nothing but symlinks** into
`/home/cbrd21/ComfyUI`, which is itself a symlink to `/mnt/comfyui/ComfyUI`:

```
drwxrwxrwx 2 cbrd21 cbrd21 4096 Mar  1 13:18 /srv/comfyui
  CODEOWNERS -> /home/cbrd21/ComfyUI/CODEOWNERS
  comfy      -> /home/cbrd21/ComfyUI/comfy
  models     -> /home/cbrd21/ComfyUI/models
  ... (all entries are symlinks)
```

NFS does not resolve symlinks on the server. The client receives the symlink
object and resolves the target in **its own** namespace. A client mounting
`/srv/comfyui` therefore gets a directory of links pointing at
`/home/cbrd21/ComfyUI/...` on the client, not on .100. This export moves no
data off .100 at all. It is decorative.

### Do not confuse the two directions

.100 is both an NFS server and an NFS client, for different shares:

| direction | server | export | client mountpoint |
|---|---|---|---|
| **outbound (this doc)** | .100 | `/srv/comfyui` | nobody |
| inbound (unrelated, healthy) | .158 | `/home/cbrd21/clawd/comfyui-shared` | .100 `/mnt/comfyui-nfs`, .41 `/mnt/comfyui-shared` |

The real shared ComfyUI corpus flows **from .158**, and both .100 and .41 are
clients of it. That is the live, working path. Nothing in this document
touches it.

## 3. The writer hunt

The fix is only durable if the script that produced the malformed heredoc is
found or ruled out. If it runs again it restores
`192.168.0.0/24(...,no_root_squash)` plus the four bogus world exports.

### Where I looked

On **.100** (`ssh cbrd21@192.168.0.100`):

| target | method | result |
|---|---|---|
| `/etc`, `/usr/local`, `/opt`, `/root`, `~/bin`, `~/.local/bin` | `sudo grep -rIl EOEXPORTS` | only `/etc/exports` (the header comment describing the incident) and `/etc/exports.bak-20260814T203233Z` (the artifact itself) |
| same roots | `sudo grep -rIl "etc/exports"` | only `/etc/init.d/nfs-kernel-server`, which is the stock Debian package script |
| `/etc/exports.d/` | `ls` | does not exist |
| `~/clawd`, `~/.skcapstone` | `find -name '*.sh' -o '*.py' -o '*.yml' -o '*.yaml' -o '*.md'` piped to `grep -lI` | 4 hits, all documentation of this incident (see below) |
| `~/.bash_history` (59 lines) | `grep -nE "EOEXPORTS|/etc/exports"` | no matches |
| `~/.zsh_history` | | file does not exist |
| cron and systemd | `systemctl list-unit-files`, `list-units --all` | no unit or timer references NFS exports. The only comfyui unit is `comfyui-model-update.timer`, which runs `~/bin/comfyui-model-update.sh` as user `cbrd21` (not root) and contains no `/etc/exports` reference |

The four `~/clawd` and `~/.skcapstone` hits are all *descriptions* of the bug,
written after it was discovered, not code that causes it:

- `clawd/skcapstone-repos/skcapstone/scripts/fleet/gen-node-disposition.py` lines 261, 272 to 274
- `clawd/skcapstone-repos/skcapstone/docs/fleet/node-100-disposition.md` lines 124, 135 to 137
- `.skcapstone/agents/lumina/context/CLAUDE.md` line 1088 (the card summary)
- `.skcapstone/coordination/reviews/2026-08-15-review.md` line 7153

On the **control node**:

| target | method | result |
|---|---|---|
| `~/clawd`, `~/.skcapstone`, `~/skworld-worktrees` | `find` over `*.sh *.bash *.py *.yml *.yaml *.json *.md *.txt` piped to `xargs grep -lI EOEXPORTS` | 25 hits, all accounted for: `gen-node-disposition.py` and `node-100-disposition.md` replicated across ten worktrees, plus the `f4fc262d` card files, the 2026-08-15 review, lumina's context `CLAUDE.md`, and this document |
| `~/.claude`, `~/.hermes` | `grep -rlI EOEXPORTS` | only this session's own files, one memory note, and two prior transcripts dated 2026-08-14 and 2026-08-15 |
| `~/.claude`, `~/.hermes` | `grep -rhoI -E "(tee|cat >|cat >>|>) ?/etc/exports"` | **no matches at all**, in any transcript |
| lumina long-term memory | `grep -rl "etc/exports"` | 4 files, all describing chipv05, a different host |

A note on method, because it is the exact trap that would have made this search
lie: searching from `~/clawd` with a `.gitignore`-aware tool silently skips all
of `skcapstone-repos`. `find` piped to `grep` was used specifically because it
does not honor `.gitignore`, so the repo trees were genuinely covered rather
than quietly excluded.

### Timestamps pin it to a single one-shot action in March

`stat` on both files reconstructs the whole event, and this is the strongest
evidence in the hunt:

| timestamp | fact |
|---|---|
| 2026-03-01 12:59:46 | `/etc/exports` **Birth**, the file is created |
| 2026-03-01 13:18:45 | `/srv/comfyui` **Birth**, the directory is created |
| 2026-03-01 13:20:11 | `/srv/comfyui` ctime, the symlinks are populated |
| **2026-03-01 13:23:19** | **`/etc/exports` last written with the malformed content** |
| 2026-08-14 20:32:33 | the backup is taken (`cp -p`, which is why it carries the March mtime) and the file is fixed |

The backup's Modify time is 2026-03-01 13:23:19 while its Birth and Change are
2026-08-14 20:32:33. That gap is the proof: the copy preserved the original
mtime, so the malformed `/etc/exports` was **written exactly once, on
2026-03-01 at 13:23:19**, four minutes after the directory it exports was
created, and never touched again for five and a half months across at least
four reboots.

A recurring job does not leave a five-and-a-half-month-old mtime. This was a
one-shot setup action in a single sitting: create `/srv/comfyui`, populate it
with symlinks, write `/etc/exports`.

### The ancestor: `tools/comfyui-nfs-setup/ollama-server-setup.sh`

The exact writer is not on disk, but its **template is**, and the match is not
subtle. `/home/cbrd21/clawd/tools/comfyui-nfs-setup/ollama-server-setup.sh`,
mtime **2026-03-01 08:01**, five hours before the malformed write at 13:23 the
same day, header comment `Run this ON the Ollama server (192.168.0.100)`,
lines 30 to 32:

```sh
cat >> /etc/exports << 'EOF'
/mnt/comfyui-share 192.168.0.0/24(rw,sync,no_subtree_check,no_root_squash)
EOF
```

The client and option string, `192.168.0.0/24(rw,sync,no_subtree_check,no_root_squash)`,
is **byte-identical** to line 1 of the malformed file. Only the exported path
differs.

It is nonetheless **not** the direct writer, on four independent grounds:

| | the script | the artifact |
|---|---|---|
| terminator | `EOF` | `EOEXPORTS` |
| path | `/mnt/comfyui-share` | `/srv/comfyui` |
| redirect | `cat >>` (append) | truncating write (the file contains only the 5 lines) |
| trailing commands | `exportfs -ra`, `systemctl restart`, then `systemctl enable` | `exportfs -ra`, `systemctl restart`, then `showmount -e`, each with a `2>/dev/null \|\| fallback` |

Its heredoc is well formed and correctly terminated, so running it cannot
produce the defect. What it establishes is provenance: someone adapted this
script by hand on 2026-03-01, renamed the terminator from `EOF` to
`EOEXPORTS`, repointed it at `/srv/comfyui`, swapped `systemctl enable` for
`showmount -e`, and added the defensive `2>/dev/null || fallback` idiom that
appears in no checked-in script on the fleet. The adaptation is where it
broke.

**Attribution of the session is unrecoverable.** .100 has no
`/var/log/auth.log` at all (journald only), the journal retains four boots
back to 2026-08-11, and `wtmp`, which begins 2026-01-28, holds no record for
2026-03-01. Who ran the command cannot be determined from the host.

### Latent hazard, and why it was not fixed here

`ollama-server-setup.sh` is still on disk and still contains a LAN-wide
`no_root_squash` export line. It targets `/mnt/comfyui-share`, which is not an
export today, and it **appends**, so re-running it on .100 would add a fresh
`192.168.0.0/24(rw,sync,no_subtree_check,no_root_squash)` entry alongside the
tightened one rather than replacing it. That is a real regression path for
this card's fix, independent of the heredoc bug.

It was not fixed in this pass because it lives in the `clawd` repo, outside
this card's worktree. **Recommended follow-up:** narrow or delete that export
block, and drop `no_root_squash`, in `clawd`. Its companion
`norap2027-mount.sh` mounts `.100:/mnt/comfyui-share`, a share that no longer
exists, so the whole `tools/comfyui-nfs-setup/` pair is stale relative to the
current topology, where the corpus flows from .158.

### Verdict: NOT FOUND, and it is not a recurring script

I did not find the writer. Stating that plainly, because it is a real result
and it changes the risk assessment rather than leaving it open.

What the search positively establishes:

- **No script anywhere contains an `EOEXPORTS` heredoc.** The regex
  `<<[^<]{0,10}EOEXPORTS` returns zero matches across every root searched, on
  .100 and on the control node. Three scripts do write `/etc/exports`
  (`ollama-server-setup.sh` above, and `runbooks/skmedia/INSTALL.md` plus its
  copy in `skstacks-v2-work`, which `tee -a` a `/media` export on .41); all
  three are ruled out on path, terminator and redirect mode. Every remaining
  `EOEXPORTS` and `/etc/exports` hit is (a) documentation of this incident
  written *after* 2026-08-14, (b) the coord card `f4fc262d` and its events, or
  (c) the stock `/etc/init.d/nfs-kernel-server`. The apparent volume of hits is
  one pair of files, `scripts/fleet/gen-node-disposition.py` and
  `docs/fleet/node-100-disposition.md`, replicated across ten worktrees.
- **No scheduler can re-run it.** No systemd unit, timer, or cron entry on
  .100 references NFS exports. The only comfyui-adjacent timer,
  `comfyui-model-update.timer`, runs `~/bin/comfyui-model-update.sh` as user
  `cbrd21`, not root, and contains no `/etc/exports` reference. A non-root
  unit cannot write `/etc/exports` in any case.
- **It is not in any session transcript.** No `tee /etc/exports`,
  `cat > /etc/exports` or equivalent appears in any Claude or Hermes
  transcript. The only two non-self transcripts mentioning `EOEXPORTS` are
  dated 2026-08-14 and 2026-08-15 and are the discovery and fix notes.
- **The evidence window does not reach March.** .100's journal retains only
  four boots, the earliest 2026-08-11, so the March 1 write is past the end of
  the logs. Claude transcripts for March are likewise not present.

Two lumina long-term memories match `/etc/exports` but describe **chipv05**,
Casey's ZFS-backed file server in his own estate (`cakjr.skworld.io`), and its
`/etc/exports.d/zfs.exports` plus a hand-written `tailnet.exports`. Different
host, different estate, different mechanism, and notably that setup
deliberately **strips** `no_root_squash`. Ruled out as the writer here.

**Most likely explanation**, consistent with everything above: a hand
adaptation of `ollama-server-setup.sh`, run once interactively over SSH during
the original ComfyUI share setup on 2026-03-01, where an outer quoting layer
(an `ssh host '...'` wrapper, or a heredoc nested inside another heredoc)
swallowed the renamed `EOEXPORTS` terminator so the redirect read to EOF. A
single-level `sudo tee /etc/exports <<'EOEXPORTS'` cannot produce this file,
because bash would have matched the terminator on line 2 and executed lines 3
to 5 as commands. For the terminator to land *inside* the file, it must have
been masked by an outer layer. That shape is produced by an interactive paste,
which is why it exists in no file and no shell history.

**Residual risk: low, but not zero, and it has two distinct parts.**

1. *Machine re-run of the exact defect: ruled out.* No script, unit, timer or
   cron entry can reproduce it, so the 2026-08-14 fix is durable against
   automation.
2. *Re-introduction of a LAN-wide `no_root_squash` export: live.*
   `ollama-server-setup.sh` still sits in `~/clawd`, still targets .100 by
   name, and still appends that export line. This is the follow-up flagged
   above and it is the more likely of the two to actually happen.

A human or agent re-running the malformed one-liner from scrollback also
cannot be excluded. The mitigation there is not a code fix: `/etc/exports` now
carries a header comment recording the incident, so the next person to open the
file is warned before overwriting it. Section 4 Option C would remove the
surface entirely.

## 4. The `no_root_squash` decision

All three client entries carry `no_root_squash`. Chef's call, not mine. Here
is the evidence and the consequences both ways.

### Evidence: what mounts this export today

**Nothing.** Five independent checks agree:

| check | result | what it proves |
|---|---|---|
| `/proc/net/rpc/nfsd` | `rpc 0 0 0 0 0`, `net 0 0 0 0` | .100's `nfsd` has served **zero RPC calls** since boot at 12:58 today, about 12 hours |
| `/proc/fs/nfsd/clients/` | empty | no NFSv4 client has any state on .100. This is the authoritative v4 check |
| `ss -tan sport = :2049` | only `LISTEN`, plus one **outbound** `192.168.0.100:863 -> 192.168.0.158:2049` | no inbound data connection from anyone. The one connection is .100 acting as a *client* of .158 |
| `.41` and `.155` `/etc/fstab`, autofs, `.mount`/`.automount` units | no entry naming 192.168.0.100 | neither listed host is configured to mount it, at boot or on demand |
| `/srv/comfyui` `stat` | mtime `2026-03-01 13:18:45`, unchanged since Birth | nothing has written to the directory in five and a half months |

A caveat, stated plainly because it is the kind of gap that makes a green check
lie: `/var/lib/nfs/rmtab` is 0 bytes with an mtime of 2024-10-02, but that is
**not** evidence on its own. `rmtab` is only populated by the NFSv3 `MOUNT`
protocol; NFSv4 has no `MOUNT` protocol and never touches it. An empty `rmtab`
on a v4-capable server is the expected state whether or not clients are
mounted. The load-bearing checks are the nfsd RPC counter and
`/proc/fs/nfsd/clients/`, which cover v4.

The only mount request `rpc.mountd` has ever logged is the 2026-08-14 20:32:58
refusal from .158, which was the previous session's negative-control test.
There is no record of a successful mount by anyone.

### Evidence: what writes to `/srv/comfyui`, and as which uid

Nothing writes to it. No process holds a file open under it (`lsof +D` empty,
zero processes with a cwd there). The directory is mode `0777` owned by
`cbrd21:cbrd21`; every entry inside is a symlink. Because the contents are
symlinks resolved client-side, there is no server-side data for a client to
write *through* in the first place.

### The actual risk `no_root_squash` carries here

`/srv` is on .100's **root filesystem**, not a separate volume:

```
/dev/mapper/ubuntu--vg-ubuntu--lv  195G  /        rw,relatime
stat: / dev=64512   /srv dev=64512   /srv/comfyui dev=64512
```

The root filesystem is mounted `rw,relatime` with **no `nosuid`**. That
completes the classic NFS local privilege escalation chain:

1. Root on .41, .100 or .155 mounts `/srv/comfyui` rw.
2. `no_root_squash` means that client's uid 0 stays uid 0 on the server, so it
   can write a file owned by root with mode 4755.
3. The file lands on .100's root filesystem, which permits setuid execution.
4. Any local unprivileged user on .100 runs `/srv/comfyui/<binary>` and is root
   on the box that serves fleet inference.

The 0777 directory mode means the write in step 2 does not even need
`no_root_squash` to succeed; what `no_root_squash` uniquely grants is the
ability to **own the file as uid 0 and set the setuid bit meaningfully**. That
is the whole of the escalation.

### Option A: set `root_squash` (drop `no_root_squash`)

- Client root maps to `anonuid=65534` (`nobody`), already the configured anon
  identity in `etab`.
- **What breaks: nothing that exists today.** No host mounts it, no process
  writes it, and the directory has not changed since March 1.
- Even in the hypothetical future where something does mount it: the directory
  is 0777, so a squashed client can still create, read and delete files there.
  Reading the symlinks needs only `x` on the directory, which `nobody` has.
  The only capability removed is creating files *owned by root*, which is
  exactly the capability that constitutes the risk.
- Residual risk after the change: effectively zero for this export.

### Option B: keep `no_root_squash`

- Preserves whatever intent motivated it. No such intent is discoverable: the
  export delivers no data, and the writer that set it cannot be found.
- Retains a setuid-root write primitive on the root filesystem of the fleet's
  GPU inference node, reachable from three LAN hosts.
- Buys nothing measurable, because nothing mounts the share.

### Option C, worth putting in front of Chef: delete the export entirely

Beyond the scope of this card, but it is the honest reading of the evidence.
The export serves a directory of symlinks that resolve on the client, so it
transfers nothing; no host is configured to mount it; nfsd has served zero
calls; and the real shared ComfyUI corpus already flows the other way, from
.158. Removing the `/srv/comfyui` line retires the `no_root_squash` question
along with the export, and closes the `EOEXPORTS` regression surface for good,
since there would be nothing left in `/etc/exports` for a re-run to corrupt
into a world export.

### Recommendation

**Option A at minimum, Option C preferred.** Both are safe on today's
evidence. Neither was applied: this is a live NFS permission change on a node
serving fleet inference that rebooted this morning after a four hour outage,
and it is Chef's call.

If Option A is chosen, the change is one line and requires `exportfs -ra`,
which does **not** restart `nfs-kernel-server` and does not touch
`ollama.service` or `skai-beellama.service`. Run
`scripts/fleet/dot100-inference-smoke.sh` before and after regardless, and
confirm `ActiveEnterTimestamp` is byte-identical for both services.

## 5. Verification performed

- `showmount -e 192.168.0.100` lists exactly `192.168.0.41`, `192.168.0.100`,
  `192.168.0.155`, unchanged from the start of this pass.
- `exportfs -v` and `/var/lib/nfs/etab` agree with `/etc/exports`.
- No write of any kind was issued to .100.
