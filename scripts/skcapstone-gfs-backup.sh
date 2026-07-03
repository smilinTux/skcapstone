#!/usr/bin/env bash
# ============================================================================
# skcapstone-gfs-backup.sh — Grandfather-Father-Son backup of ~/.skcapstone
# ============================================================================
# Backs up the IRREPLACEABLE sovereign state as compressed tarballs on a GFS
# rotation. Excludes rebuildable/transient bulk (Chroma vector store, index.db,
# WAL, worship-session media, comms queues, logs, skwhisper cache) — those
# reconstruct from the flat memory tiers on restore.
#
# Retention (GFS):
#   Daily   (Son)         : keep 14   (runs every day)
#   Weekly  (Father)      : keep  8   (promoted on Sundays)
#   Monthly (Grandfather) : keep 12   (promoted on the 1st)
#   Yearly                : keep  2   (promoted on Jan 1)
#
# Compressed core is ~0.4-0.5 GB, so full rotation steady-state is ~15-18 GB.
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

# --- retention depths ---
DAILY_KEEP=14
WEEKLY_KEEP=8
MONTHLY_KEEP=12
YEARLY_KEEP=2

# --- min free space (KB) required before we write a new backup ---
MIN_FREE_KB=$((2 * 1024 * 1024))   # 2 GB

mkdir -p "$BACKUP_BASE"/{daily,weekly,monthly,yearly} "$(dirname "$LOG")"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }

log "=== GFS backup starting ==="

# --- disk guard -------------------------------------------------------------
FREE_KB=$(df --output=avail "$SRC_ROOT" | tail -1 | tr -d ' ')
if [ "$FREE_KB" -lt "$MIN_FREE_KB" ]; then
  log "ABORT: only $((FREE_KB/1024)) MB free (< $((MIN_FREE_KB/1024)) MB). Skipping backup."
  command -v sk-alert >/dev/null 2>&1 && \
    sk-alert "⚠️ skcapstone GFS backup skipped — low disk ($((FREE_KB/1024)) MB free)" >/dev/null 2>&1 || true
  exit 1
fi

# --- create the daily tarball ----------------------------------------------
DAILY_FILE="$BACKUP_BASE/daily/skcapstone-state-${TS}.tar.gz"

# Archive $HOME/.skcapstone as ".skcapstone/..." so restore is unambiguous.
# NOTE: '*/backups' excludes this very GFS tree (no recursive nesting).
tar -czf "$DAILY_FILE" -C "$HOME" \
  --exclude='.skcapstone/agent' \
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
  .skcapstone

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
log "=== GFS backup complete ==="
