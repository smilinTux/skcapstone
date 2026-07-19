# Backup - sovereign state, on a rotation

SKCapstone gives you two complementary tools for protecting agent state:

- **`skcapstone backup`** - a portable, self-contained tarball of one agent
  home (identity, memories, trust, config, coordination, card). Restore it on
  any machine and your agent travels with you.
- **The recommended GFS cron** (`scripts/skcapstone-gfs-backup.sh`) - an
  operator-side scheduled backup of the **irreplaceable** parts of
  `~/.skcapstone` on a Grandfather-Father-Son rotation, so you always have
  depth (yesterday, last week, last month, last year) without unbounded growth.

This is the inverse of [Housekeeping](HOUSEKEEPING.md): housekeeping *prunes the
transient files you never read again*; backup *preserves the state you can never
regenerate*. Run both.

---

## The `skcapstone backup` CLI

```bash
skcapstone backup create              # tarball the active agent's home
skcapstone backup create --agent opus # a specific agent
skcapstone backup create -o /mnt/usb/backups
skcapstone backup list                # list backups
skcapstone backup restore <archive>   # restore onto any machine
```

`create` archives the active agent's per-agent home
(`~/.skcapstone/agents/<name>/`) - identity, the flat memory tiers, trust,
config, coordination, and the agent card - with integrity checksums. Because it
also captures the on-disk vector store and SQLite index, a full home tarball can
be large (~1 GB+). That is fine for a one-off portability snapshot, but too
heavy to keep dozens of on a rotation. For scheduled retention, use the GFS cron
below, which drops the rebuildable bulk.

---

## Recommended scheduled backup (GFS)

Ship `scripts/skcapstone-gfs-backup.sh` to the operator home and schedule it
daily. It writes compressed, checksummed tarballs of the irreplaceable state and
rotates them on the classic **Grandfather-Father-Son** scheme:

| Tier | Keep | Promoted |
| ---- | ---- | -------- |
| **daily** (Son) | 14 | every run |
| **weekly** (Father) | 8 | Sundays |
| **monthly** (Grandfather) | 12 | 1st of month |
| **yearly** | 2 | Jan 1 |

Retention depths live at the top of the script - tune `DAILY_KEEP`,
`WEEKLY_KEEP`, `MONTHLY_KEEP`, `YEARLY_KEEP` to taste.

### What it backs up vs. skips

It archives `~/.skcapstone` - every agent's **flat memory tiers**
(`short/mid/long-term`), soul, identity, trust, seeds, config, coordination,
journal, song anchors - and **excludes the rebuildable or transient bulk**:

- Chroma vector store, `index.db` + WAL - a *local working index* rebuilt from
  the flat memory tiers (see [Memory Architecture](ARCHITECTURE.md)).
- Worship-session media, `voices/` - regenerable renders.
- Comms queues (`inbox`/`outbox`/`acks`), `logs/`, `skwhisper/` cache - transient.
- venvs, `__pycache__`, `.stversions`, `node_modules`, lock/pid/tmp/WAL files.
- Nested `backups/` (so the rotation never archives itself).

The result is small - a ~0.8 GB home compresses to **~80 MB** - so the whole
14/8/12/2 rotation is only a few GB and stays bounded by the pruner.

### Install

```bash
install -m755 scripts/skcapstone-gfs-backup.sh ~/.skcapstone/scripts/

# daily at 02:45 - pick a quiet slot distinct from the housekeeping window
( crontab -l 2>/dev/null; \
  echo '45 2 * * * /bin/bash '"$HOME"'/.skcapstone/scripts/skcapstone-gfs-backup.sh >/dev/null 2>&1' \
) | crontab -
```

Backups land in `~/.skcapstone/backups/gfs/{daily,weekly,monthly,yearly}/`, each
`*.tar.gz` beside a matching `*.sha256`. Progress is logged to
`~/.skcapstone/logs/skcapstone-gfs-backup.log`.

### Off-site replication (3-2-1, opt-in)

Local rotation is the floor; a copy **off the box** is what survives disk or
machine loss. Set an rsync/ssh target in `~/.skcapstone/config/backup.env` and
every run mirrors the whole rotation to it:

```bash
# ~/.skcapstone/config/backup.env   (chmod 600 - keep operator hosts out of the repo)
OFFSITE_DEST="othermachine:/home/you/skcapstone-offsite/<thisbox>"
```

- Uses key-based ssh (`BatchMode`) - no passwords in cron; set up a key to the
  target first.
- **Best-effort:** a failed push logs + fires `sk-alert` but never fails the
  local backup - you still have today's archive on disk.
- Mirrors with `rsync -a --delete`, so the off-site copy tracks the same GFS
  retention (pruned tiers are pruned there too). Checksums travel with the
  archives, so you can `sha256sum -c` on the far side.
- Unset ⇒ local-only (logged as such).

Encrypt the off-site copy at rest if the target isn't trusted - the flat memory
tiers are plaintext-on-disk by default.

What the off-site copy protects is the **source of truth**: the flat memory tiers
(and, alongside them, the git wiki). The `skmem-pg` index is not shipped off-box,
because it is a per-node derived index that any node rebuilds from that source. A
`skmem-pg` `pg_dump` is optional and backup-only - a faster warm-start convenience,
never something the restore path depends on.

### Safety

- **Disk guard.** The script refuses to run (and fires `sk-alert` if present)
  when free space drops below `MIN_FREE_KB` (default 2 GB), so a full disk never
  gets pushed over the edge by a backup.
- **Never follows symlinks** out of `~/.skcapstone`; the `agent → agents`
  convenience symlink is skipped so nothing is archived twice.
- **Idempotent cron install** - grep for the script before adding the line so
  re-running the installer doesn't duplicate it.

---

## Restore

```bash
cd ~/.skcapstone/backups/gfs/daily
sha256sum -c skcapstone-state-YYYYMMDD-HHMMSS.tar.gz.sha256   # verify first
tar -xzf skcapstone-state-YYYYMMDD-HHMMSS.tar.gz -C /restore/root
# rebuild the local SQLite index from the restored flat memory tiers:
skmemory reindex        # or: skcapstone doctor
```

Because the GFS backup deliberately omits the vector store and SQLite index,
**rebuild the derived indexes after restoring** - they all reconstruct from
source, they are not part of the archive.

`skmem-pg` is a per-node, local, rebuildable derived index (not a
streaming-replicated or shared store), so a restored node rebuilds it in place
rather than shipping a database over the wire. Two independent rebuild paths, both
from source:

- **`memories` table** ← rebuilt from the flat memory tiers (which *are* in the
  archive) by the reconcile engine (`skmem_reconcile.py`, run per agent). This is
  idempotent and agent-scoped, and re-embeds any null vectors via mxbai on `.100`.
- **`docs` / `file_locations` (wiki-canon)** ← re-ingested per-node by skingest
  from the git-synced wiki.

A `skmem-pg` `pg_dump` is **optional and backup-only**: it is a convenience for a
faster warm start, never the system of record. The authoritative source is always
the synced flat files plus the git wiki, so a node can rebuild the full index
without any dump.

---

## Which do I use?

| Need | Use |
| ---- | --- |
| Move an agent to a new machine | `skcapstone backup create` → `restore` |
| Nightly point-in-time history with depth | GFS cron (`skcapstone-gfs-backup.sh`) |
| Reclaim space from transient churn | [`skcapstone housekeeping`](HOUSEKEEPING.md) |
