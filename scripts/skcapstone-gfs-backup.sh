#!/usr/bin/env bash

# --- cron PATH guard --------------------------------------------------------
# Under cron the PATH is minimal, so `command -v sk-alert` returned false and
# every alert in this script silently no-opped. That is why the 2026-10-14
# "ABORT: only 500 MB free" never reached anyone and backups sat broken for a
# day with no signal. Resolve the binary by absolute path instead of trusting
# PATH, and keep ~/.skenv/bin on PATH for anything else this script shells to.
#
# sk-alert now ships with skcapstone itself (PR #1301), so this path is the
# packaged one rather than a symlink into an agent runtime.
export PATH="$HOME/.skenv/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
SK_ALERT_BIN="$HOME/.skenv/bin/sk-alert"
sk_notify() {
  [ -x "$SK_ALERT_BIN" ] && "$SK_ALERT_BIN" "$1" >/dev/null 2>&1 || true
}
# ============================================================================
# skcapstone-gfs-backup.sh: GFS backup of ~/.skcapstone plus ~/.hermes config
# ============================================================================
# Backs up the IRREPLACEABLE sovereign state as compressed tarballs on a GFS
# rotation. Excludes rebuildable/transient bulk (Chroma vector store, index.db,
# WAL, worship-session media, comms queues, logs, skwhisper cache) — those
# reconstruct from the flat memory tiers on restore.
#
# Retention (GFS):
#   Daily   (Son)         : keep 14   (runs every day)
#   Weekly  (Father)      : keep  8   (promoted on Sundays)
#   ...all four overridable in config/backup.env; these are defaults, and the
#   header is no longer the place a node records a local change.
#   Monthly (Grandfather) : keep 12   (promoted on the 1st)
#   Yearly                : keep  2   (promoted on Jan 1)
#
# Compressed core is ~0.4-0.5 GB, so full rotation steady-state is ~15-18 GB.
#
# Off-site (3-2-1, opt-in):  set  OFFSITE_DEST="<host>:<path>"  in
#   ~/.skcapstone/config/backup.env  to also rsync the whole rotation to another
#   host each run (best-effort — a failed push alerts but never fails the backup).
#
# Restore:  tar -xzf <archive> -C /path/to/restore-root
#   then rebuild the vector index:  skmemory reindex   (or skcapstone doctor)
# ============================================================================

set -euo pipefail

SRC_ROOT="$HOME/.skcapstone"
BACKUP_BASE="$SRC_ROOT/backups/gfs"
LOG="$SRC_ROOT/logs/skcapstone-gfs-backup.log"

DATE=$(date +%Y-%m-%d)
DOW=$(date +%u)   # 1=Mon .. 7=Sun
DOM=$(date +%d)   # 01..31
DOY=$(date +%j)   # 001..366
TS=$(date +%Y%m%d-%H%M%S)

# --- node config ------------------------------------------------------------
# Sourced here rather than only before the off-site push, so a node can
# override retention as well as OFFSITE_DEST.
CONF="$SRC_ROOT/config/backup.env"
# shellcheck source=/dev/null
[ -f "$CONF" ] && . "$CONF"

# --- retention depths -------------------------------------------------------
# Overridable from config/backup.env (sourced below), because a node that is
# tight on disk should not have to edit this file. noroc2027 was running 7 and 5
# against these defaults of 14 and 8, with the header still advertising 14, so
# anyone reading it believed they had twice the daily depth they had. An edited
# script is also how the cron-PATH and tar-tolerance fixes below ended up
# existing on exactly one machine and in no repo.
DAILY_KEEP="${DAILY_KEEP:-14}"
WEEKLY_KEEP="${WEEKLY_KEEP:-8}"
MONTHLY_KEEP="${MONTHLY_KEEP:-12}"
YEARLY_KEEP="${YEARLY_KEEP:-2}"

# --- min free space (KB) required before we write a new backup ---
MIN_FREE_KB=$((2 * 1024 * 1024))   # 2 GB

mkdir -p "$BACKUP_BASE"/{daily,weekly,monthly,yearly} "$(dirname "$LOG")"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }

log "=== GFS backup starting ==="

# --- disk guard -------------------------------------------------------------
FREE_KB=$(df --output=avail "$SRC_ROOT" | tail -1 | tr -d ' ')
if [ "$FREE_KB" -lt "$MIN_FREE_KB" ]; then
  log "ABORT: only $((FREE_KB/1024)) MB free (< $((MIN_FREE_KB/1024)) MB). Skipping backup."
  sk_notify "⚠️ skcapstone GFS backup skipped: low disk ($((FREE_KB/1024)) MB free) on $(hostname)"
  exit 1
fi

# --- ~/.hermes config slice -------------------------------------------------
# ~/.hermes is NOT a git repo and was in no backup set, yet it holds the Hermes
# config and its dated backups, the Nextcloud Talk and Telegram wiring, and the
# cron job definitions. Losing that directory loses an estate's comms config,
# and nothing would have restored it.
#
# Config and logic ONLY. The runtime bulk is 6.8 GB on noroc2027: state.db
# alone is 1.3 GB and compresses to 438 MB, it rebuilds, and copying a live
# SQLite into a tarball yields a torn file anyway. With the excludes below the
# slice measured ~3.6 MB compressed, 1539 entries.
#
# SECRETS ARE EXCLUDED BY NAME (.env*, secrets/, auth.json), because the
# off-site push sends this archive to another host. Verify after any change:
#   tar -tzf <archive> | grep -cE '\.env|secrets/|auth\.json'    # want 0
#
# If this slice ever balloons, something new and heavy landed in ~/.hermes.
# Add it here rather than dropping the whole tree from the backup again.
# A node without ~/.hermes must still back up: naming a missing path makes tar
# exit 2, which this script treats as fatal, so the operand is added only when
# the directory is there. Most chi nodes have no Hermes at all.
HERMES_EXCLUDES=(
  # rebuildable caches and vendored payloads
  --exclude='.hermes/cache'
  --exclude='.hermes/installs'
  --exclude='.hermes/tools'
  --exclude='.hermes/bin'
  --exclude='.hermes/lsp'
  --exclude='.hermes/skills'
  --exclude='.hermes/plugins'
  --exclude='.hermes/models_dev_cache.json'
  # media and transcript caches
  --exclude='.hermes/video_cache'
  --exclude='.hermes/audio_cache'
  --exclude='.hermes/sessions'
  # runtime state (regenerates; state.db is 1.3 GB and always in flight)
  --exclude='.hermes/state'
  --exclude='.hermes/state-snapshots'
  --exclude='.hermes/checkpoints'
  --exclude='.hermes/profiles'
  --exclude='.hermes/migration'
  --exclude='.hermes/*.db'
  --exclude='.hermes/cron/*.db'
  # secrets: never leave this host in an archive
  --exclude='.hermes/.env*'
  --exclude='.hermes/secrets'
  --exclude='.hermes/auth.json'
)

# Only back up trees that exist on this node.
TAR_PATHS=(.skcapstone)
[ -d "$HOME/.hermes" ] && TAR_PATHS+=(.hermes) || :

# --- create the daily tarball ----------------------------------------------
DAILY_FILE="$BACKUP_BASE/daily/skcapstone-state-${TS}.tar.gz"

# Archive $HOME/.skcapstone as ".skcapstone/..." so restore is unambiguous.
# NOTE: '*/backups' excludes this very GFS tree (no recursive nesting).
tar_rc=0
tar -czf "$DAILY_FILE" -C "$HOME" \
  --exclude='.skcapstone/agent' \
  --exclude='.skcapstone/config/alert.secret' \
  --exclude='*/memory/chroma' \
  --exclude='*/memory/chroma.bak*' \
  --exclude='*/memory/index.db*' \
  --exclude='*/memory/wal' \
  --exclude='*/memory/worship-sessions' \
  --exclude='*/memory/archive' \
  --exclude='*/backups' \
  --exclude='*/logs' \
  --exclude='*/skwhisper' \
  --exclude='*/voices' \
  --exclude='*/inbox' \
  --exclude='*/outbox' \
  --exclude='*/acks' \
  --exclude='*/venv' \
  --exclude='*/__pycache__' \
  --exclude='*/.stversions' \
  --exclude='.stfolder' \
  --exclude='*/node_modules' \
  --exclude='*/.git' \
  --exclude='*.db-wal' \
  --exclude='*.db-shm' \
  --exclude='*.lock' \
  --exclude='*.pid' \
  --exclude='*.tmp' \
  --exclude='*.sync-conflict*' \
  "${HERMES_EXCLUDES[@]}" \
  "${TAR_PATHS[@]}" || tar_rc=$?

# --- tolerate benign tar warnings -------------------------------------------
# This script runs `set -euo pipefail` against a LIVE tree, so any file that
# changes while tar reads it makes tar exit 1 and killed the whole run right
# here: after the tarball was written but BEFORE its checksum, the GFS
# rotation and the off-site push. Observed 2026-10-15 on
# .skcapstone/pubsub/topics/skgateway.warn, which a running service appends to.
# The result was the worst kind of failure, a 660 MB archive on disk with no
# sha256, never rotated, never pushed, and a log that simply stopped mid-run.
# tar exit 1 means "warnings, archive written"; 2+ means fatal.
if [ "${tar_rc:-0}" -gt 1 ]; then
  log "ABORT: tar failed fatally (exit $tar_rc)"
  sk_notify "⚠️ skcapstone GFS backup FAILED: tar exit $tar_rc on $(hostname)"
  exit 1
fi
if [ "${tar_rc:-0}" -eq 1 ]; then
  log "NOTE: tar reported changed-while-reading warnings (exit 1); archive is usable"
fi

# integrity checksum sidecar
( cd "$(dirname "$DAILY_FILE")" && sha256sum "$(basename "$DAILY_FILE")" > "$(basename "$DAILY_FILE").sha256" )

SIZE=$(du -h "$DAILY_FILE" | cut -f1)
log "Daily backup: $DAILY_FILE ($SIZE)"

# --- promote to weekly / monthly / yearly ----------------------------------
if [ "$DOW" = "7" ]; then
  cp -p "$DAILY_FILE"        "$BACKUP_BASE/weekly/skcapstone-state-week-${DATE}.tar.gz"
  cp -p "$DAILY_FILE.sha256" "$BACKUP_BASE/weekly/skcapstone-state-week-${DATE}.tar.gz.sha256" 2>/dev/null || true
  log "Promoted → weekly (skcapstone-state-week-${DATE}.tar.gz)"
fi
if [ "$DOM" = "01" ]; then
  cp -p "$DAILY_FILE"        "$BACKUP_BASE/monthly/skcapstone-state-month-${DATE}.tar.gz"
  cp -p "$DAILY_FILE.sha256" "$BACKUP_BASE/monthly/skcapstone-state-month-${DATE}.tar.gz.sha256" 2>/dev/null || true
  log "Promoted → monthly (skcapstone-state-month-${DATE}.tar.gz)"
fi
if [ "$DOY" = "001" ]; then
  cp -p "$DAILY_FILE"        "$BACKUP_BASE/yearly/skcapstone-state-year-${DATE}.tar.gz"
  cp -p "$DAILY_FILE.sha256" "$BACKUP_BASE/yearly/skcapstone-state-year-${DATE}.tar.gz.sha256" 2>/dev/null || true
  log "Promoted → yearly (skcapstone-state-year-${DATE}.tar.gz)"
fi

# --- rotation cleanup -------------------------------------------------------
# prune <dir> <glob> <keep>  — keep newest N, delete the rest (+ their .sha256)
prune() {
  local dir="$1" glob="$2" keep="$3" f
  cd "$dir" || return 0
  # shellcheck disable=SC2012
  for f in $(ls -t $glob 2>/dev/null | tail -n +$((keep + 1)) || true); do
    rm -f "$f" "$f.sha256"
  done
  return 0
}

prune "$BACKUP_BASE/daily"   "skcapstone-state-2*.tar.gz"       "$DAILY_KEEP"
prune "$BACKUP_BASE/weekly"  "skcapstone-state-week-*.tar.gz"   "$WEEKLY_KEEP"
prune "$BACKUP_BASE/monthly" "skcapstone-state-month-*.tar.gz"  "$MONTHLY_KEEP"
prune "$BACKUP_BASE/yearly"  "skcapstone-state-year-*.tar.gz"   "$YEARLY_KEEP"

count() { find "$1" -maxdepth 1 -name '*.tar.gz' 2>/dev/null | wc -l; }
D=$(count "$BACKUP_BASE/daily")
W=$(count "$BACKUP_BASE/weekly")
M=$(count "$BACKUP_BASE/monthly")
Y=$(count "$BACKUP_BASE/yearly")
TOTAL=$(du -sh "$BACKUP_BASE" 2>/dev/null | cut -f1)

log "Rotation complete: ${D} daily, ${W} weekly, ${M} monthly, ${Y} yearly (total ${TOTAL})"

# --- off-site replication (opt-in, 3-2-1) -----------------------------------
# Set OFFSITE_DEST="<host>:<path>" (an ssh/rsync target) in config/backup.env
# to mirror the whole rotation off the box. Unset ⇒ local-only (skipped).
if [ -n "${OFFSITE_DEST:-}" ]; then
  OFF_HOST="${OFFSITE_DEST%%:*}"
  OFF_PATH="${OFFSITE_DEST#*:}"
  SSH_OPTS="ssh -o BatchMode=yes -o ConnectTimeout=10"
  if $SSH_OPTS "$OFF_HOST" "mkdir -p '$OFF_PATH/gfs'" >>"$LOG" 2>&1 && \
     rsync -a --delete -e "$SSH_OPTS" "$BACKUP_BASE/" "$OFF_HOST:$OFF_PATH/gfs/" >>"$LOG" 2>&1; then
    log "Off-site push OK → $OFFSITE_DEST"
  else
    log "Off-site push FAILED → $OFFSITE_DEST"
    sk_notify "⚠️ skcapstone off-site backup push to $OFF_HOST FAILED on $(hostname)"
  fi
else
  log "Off-site: not configured (set OFFSITE_DEST in config/backup.env for 3-2-1)"
fi

log "=== GFS backup complete ==="
