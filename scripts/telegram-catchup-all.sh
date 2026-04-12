#!/usr/bin/env bash
# telegram-catchup-all.sh — Import all configured Telegram groups into SKMemory
#
# Reads groups from ~/.skcapstone/agents/lumina/config/telegram.yaml
# and runs `skcapstone telegram catchup` for each enabled group.
#
# Usage:
#   bash scripts/telegram-catchup-all.sh [--since YYYY-MM-DD] [--limit N] [--group NAME]
#
# Examples:
#   bash scripts/telegram-catchup-all.sh                    # All groups, last 2000 msgs
#   bash scripts/telegram-catchup-all.sh --since 2026-03-01 # All groups since March 1
#   bash scripts/telegram-catchup-all.sh --group brother-john  # Just one group
#
# Requires:
#   - TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables
#   - ~/.skenv/bin/skcapstone on PATH
#   - Telethon installed in ~/.skenv/

set -uo pipefail  # no -e: individual group failures shouldn't stop the batch

SKENV="${HOME}/.skenv/bin"
SKCAPSTONE="${SKENV}/skcapstone"
CONFIG="${HOME}/.skcapstone/agents/lumina/config/telegram.yaml"
export SKAGENT="${SKAGENT:-lumina}"
export SKCAPSTONE_AGENT="${SKAGENT}"
export PATH="${SKENV}:${PATH}"

# Parse args
SINCE=""
LIMIT="2000"
ONLY_GROUP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --group) ONLY_GROUP="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Check prerequisites
if [[ -z "${TELEGRAM_API_ID:-}" || -z "${TELEGRAM_API_HASH:-}" ]]; then
  echo "ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set."
  echo "Get them from https://my.telegram.org"
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "ERROR: Config not found: $CONFIG"
  exit 1
fi

# Parse groups from YAML (simple grep — no yq dependency)
echo "=== Telegram Catch-Up All ==="
echo "Config: $CONFIG"
echo "Agent: $SKCAPSTONE_AGENT"
echo "Limit: $LIMIT"
[[ -n "$SINCE" ]] && echo "Since: $SINCE"
[[ -n "$ONLY_GROUP" ]] && echo "Only group: $ONLY_GROUP"
echo ""

# Extract group entries: name, chat ID, tags, enabled status
SUCCESS=0
FAILED=0
SKIPPED=0

current_name=""
current_chat=""
current_tags=""
current_enabled=""

process_group() {
  local name="$1" chat="$2" tags="$3" enabled="$4"

  if [[ "$enabled" != "true" ]]; then
    echo "  SKIP $name (disabled)"
    SKIPPED=$((SKIPPED + 1))
    return
  fi

  if [[ -n "$ONLY_GROUP" && "$name" != *"$ONLY_GROUP"* ]]; then
    SKIPPED=$((SKIPPED + 1))
    return
  fi

  echo -n "  IMPORTING $name (chat: $chat) ... "

  local cmd="$SKCAPSTONE telegram catchup $chat --limit $LIMIT --min-length 20"
  [[ -n "$SINCE" ]] && cmd="$cmd --since $SINCE"
  [[ -n "$tags" ]] && cmd="$cmd --tags $tags"

  if eval "$cmd" > /tmp/telegram-catchup-$name.log 2>&1; then
    echo "OK"
    SUCCESS=$((SUCCESS + 1))
  else
    echo "FAILED (see /tmp/telegram-catchup-$name.log)"
    FAILED=$((FAILED + 1))
  fi

  # Rate limit — avoid hitting Telegram flood control
  sleep 3
}

# Parse the YAML manually
while IFS= read -r line; do
  # Detect new group entry
  if [[ "$line" =~ ^[[:space:]]*-[[:space:]]*name:[[:space:]]*(.*) ]]; then
    # Process previous group if we have one
    if [[ -n "$current_name" ]]; then
      process_group "$current_name" "$current_chat" "$current_tags" "$current_enabled"
    fi
    current_name="${BASH_REMATCH[1]}"
    current_chat=""
    current_tags=""
    current_enabled="true"
  elif [[ "$line" =~ ^[[:space:]]*chat:[[:space:]]*\"?([0-9]+)\"? ]]; then
    current_chat="${BASH_REMATCH[1]}"
  elif [[ "$line" =~ ^[[:space:]]*tags:[[:space:]]*\[(.*)\] ]]; then
    # Convert YAML list to comma-separated
    current_tags=$(echo "${BASH_REMATCH[1]}" | sed 's/,/ /g' | tr -s ' ' ',' | sed 's/^,//;s/,$//')
  elif [[ "$line" =~ ^[[:space:]]*enabled:[[:space:]]*(.*) ]]; then
    current_enabled="${BASH_REMATCH[1]}"
  fi
done < "$CONFIG"

# Process last group
if [[ -n "$current_name" ]]; then
  process_group "$current_name" "$current_chat" "$current_tags" "$current_enabled"
fi

echo ""
echo "=== Done ==="
echo "  Success: $SUCCESS"
echo "  Failed:  $FAILED"
echo "  Skipped: $SKIPPED"
