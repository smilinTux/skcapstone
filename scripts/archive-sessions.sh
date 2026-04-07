#!/bin/bash
# archive-sessions.sh
# Archive OpenClaw session files that are older than 24h or larger than 200KB.
# Keeps the 5 most recently modified .jsonl files regardless of size/age.
# Safe to run multiple times (idempotent).

set -euo pipefail

SESSION_DIR="$HOME/.openclaw/agents/lumina/sessions"
ARCHIVE_DIR="$SESSION_DIR/archive"
MAX_SIZE_KB=200
MAX_AGE_HOURS=24
KEEP_RECENT=5

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }

# Cross-platform stat helpers
_stat_mtime() {
    if [[ "$OSTYPE" == "darwin"* ]]; then stat -f '%m' "$1"; else stat -c '%Y' "$1"; fi
}
_stat_size() {
    if [[ "$OSTYPE" == "darwin"* ]]; then stat -f '%z' "$1"; else stat -c '%s' "$1"; fi
}

# Ensure directories exist
if [ ! -d "$SESSION_DIR" ]; then
    log "Session directory does not exist: $SESSION_DIR — nothing to do."
    exit 0
fi
mkdir -p "$ARCHIVE_DIR"

# Collect all .jsonl files (not in archive subdir), sorted newest-first
# (macOS-compatible: avoid mapfile and find -printf)
all_files=()
while IFS= read -r f; do
    [ -n "$f" ] && all_files+=("$f")
done < <(
    for f in "$SESSION_DIR"/*.jsonl; do
        [ -f "$f" ] && echo "$(_stat_mtime "$f") $f"
    done | sort -rn | awk '{print $2}'
)

total=${#all_files[@]}
if [ "$total" -eq 0 ]; then
    log "No .jsonl files found — nothing to do."
    exit 0
fi

log "Found $total .jsonl file(s) in $SESSION_DIR"

# The first KEEP_RECENT entries (newest) are protected
archived=0
for i in "${!all_files[@]}"; do
    file="${all_files[$i]}"
    basename_f="$(basename "$file")"

    # Skip if already archived (shouldn't happen with maxdepth 1, but be safe)
    if [ "$(dirname "$file")" = "$ARCHIVE_DIR" ]; then
        continue
    fi

    # Protect the N most recent files
    if [ "$i" -lt "$KEEP_RECENT" ]; then
        log "KEEP (recent #$((i+1))): $basename_f"
        continue
    fi

    # Check age (older than MAX_AGE_HOURS)
    file_age_sec=$(( $(date +%s) - $(_stat_mtime "$file") ))
    old_enough=$(( file_age_sec > MAX_AGE_HOURS * 3600 ))

    # Check size (larger than MAX_SIZE_KB)
    file_size_kb=$(( $(_stat_size "$file") / 1024 ))
    big_enough=$(( file_size_kb >= MAX_SIZE_KB ))

    if [ "$old_enough" -eq 1 ] || [ "$big_enough" -eq 1 ]; then
        reason=""
        [ "$old_enough" -eq 1 ] && reason="age=$(( file_age_sec / 3600 ))h"
        [ "$big_enough" -eq 1 ] && { [ -n "$reason" ] && reason="$reason, "; reason="${reason}size=${file_size_kb}KB"; }
        log "ARCHIVE ($reason): $basename_f"
        # Save session to skmemory before archiving
        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        SESSION_TO_MEM="$SCRIPT_DIR/session-to-memory.py"
        if [ -f "$SESSION_TO_MEM" ]; then
            log "  → saving session digest to skmemory..."
            python3 "$SESSION_TO_MEM" "$file" --agent lumina 2>&1 | while IFS= read -r l; do log "    $l"; done || true
        fi
        mv -- "$file" "$ARCHIVE_DIR/$basename_f"
        archived=$((archived + 1))
    else
        log "SKIP (below thresholds): $basename_f"
    fi
done

log "Done. Archived $archived file(s)."
