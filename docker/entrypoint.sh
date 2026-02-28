#!/usr/bin/env bash
# Sovereign Agent Container Entrypoint
#
# Reads /agent/config.json written by DockerProvider.configure(), builds
# the MCP config for the claude CLI, then launches the agent session.
#
# Exit codes:
#   0  — agent session completed normally
#   1  — configuration error (config.json missing after timeout)
#   2  — claude CLI not found

set -euo pipefail

AGENT_NAME="${AGENT_NAME:-unknown}"
TEAM_NAME="${TEAM_NAME:-default}"
AGENT_ROLE="${AGENT_ROLE:-worker}"
AGENT_MODEL="${AGENT_MODEL:-claude-haiku-4-5-20251001}"
CONFIG_FILE="/agent/config.json"
MCP_CONFIG_FILE="/agent/mcp_config.json"
STATE_FILE="/agent/session_state.json"
LOG_FILE="/agent/agent.log"
SKCOMM_HOME="${SKCOMM_HOME:-/skcomm}"
INBOX_DIR="${SKCOMM_HOME}/${TEAM_NAME}/${AGENT_NAME}/inbox"
OUTBOX_DIR="${SKCOMM_HOME}/${TEAM_NAME}/${AGENT_NAME}/outbox"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    echo "[$(date -u +%FT%TZ)] [entrypoint] $*" | tee -a "$LOG_FILE" >&2
}

write_state() {
    local status="$1"
    local msg="${2:-}"
    printf '{"status":"%s","agent_name":"%s","timestamp":"%s"%s}\n' \
        "$status" "$AGENT_NAME" "$(date -u +%FT%TZ)" \
        "${msg:+,\"message\":\"$msg\"}" \
        > "$STATE_FILE"
}

# ---------------------------------------------------------------------------
# Wait for config.json (DockerProvider.configure() writes it asynchronously)
# ---------------------------------------------------------------------------

log "Starting agent $AGENT_NAME (role=$AGENT_ROLE model=$AGENT_MODEL)"
write_state "starting"

if [ ! -f "$CONFIG_FILE" ]; then
    log "Waiting up to 60s for $CONFIG_FILE …"
    timeout 60 bash -c "until [ -f '$CONFIG_FILE' ]; do sleep 1; done" \
        || { log "ERROR: config.json never appeared"; write_state "error" "config missing"; exit 1; }
fi

log "Config loaded: $CONFIG_FILE"

# ---------------------------------------------------------------------------
# Build MCP config for skcapstone sovereign stack
# ---------------------------------------------------------------------------

mkdir -p "$(dirname "$MCP_CONFIG_FILE")"

# Prefer TCP host; fall back to Unix socket; fall back to local stdio
if [ -n "${SKCAPSTONE_MCP_HOST:-}" ]; then
    log "MCP via TCP: $SKCAPSTONE_MCP_HOST"
    cat > "$MCP_CONFIG_FILE" <<-EOF
{
  "mcpServers": {
    "skcapstone": {
      "command": "skcapstone",
      "args": ["mcp", "--stdio"],
      "env": {
        "SKCAPSTONE_MCP_REMOTE": "${SKCAPSTONE_MCP_HOST}"
      }
    }
  }
}
EOF
elif [ -S "${SKCAPSTONE_MCP_SOCKET:-/run/skcapstone/mcp.sock}" ]; then
    log "MCP via socket: ${SKCAPSTONE_MCP_SOCKET:-/run/skcapstone/mcp.sock}"
    cat > "$MCP_CONFIG_FILE" <<-EOF
{
  "mcpServers": {
    "skcapstone": {
      "command": "skcapstone",
      "args": ["mcp", "--stdio"],
      "env": {
        "SKCAPSTONE_SOCKET": "${SKCAPSTONE_MCP_SOCKET:-/run/skcapstone/mcp.sock}"
      }
    }
  }
}
EOF
else
    log "MCP via local stdio (no remote configured)"
    cat > "$MCP_CONFIG_FILE" <<-EOF
{
  "mcpServers": {
    "skcapstone": {
      "command": "skcapstone",
      "args": ["mcp", "--stdio"]
    }
  }
}
EOF
fi

# ---------------------------------------------------------------------------
# Resolve soul blueprint
# ---------------------------------------------------------------------------

SOUL_PATH=""
if [ -n "${SOUL_BLUEPRINT:-}" ]; then
    if [ -f "$SOUL_BLUEPRINT" ] || [ -d "$SOUL_BLUEPRINT" ]; then
        SOUL_PATH="$SOUL_BLUEPRINT"
    elif [ -f "/agent/souls/${SOUL_BLUEPRINT}" ]; then
        SOUL_PATH="/agent/souls/${SOUL_BLUEPRINT}"
    fi
fi

# ---------------------------------------------------------------------------
# Build system prompt from soul blueprint + agent context
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_FILE="/agent/system_prompt.txt"
{
    if [ -n "$SOUL_PATH" ]; then
        if [ -f "$SOUL_PATH" ]; then
            cat "$SOUL_PATH"
        elif [ -d "$SOUL_PATH" ]; then
            for f in "$SOUL_PATH"/*.md "$SOUL_PATH"/*.txt 2>/dev/null; do
                [ -f "$f" ] && cat "$f"
            done
        fi
        echo ""
    fi
    echo "## Agent Context"
    echo ""
    echo "- **Name**: ${AGENT_NAME}"
    echo "- **Role**: ${AGENT_ROLE}"
    echo "- **Team**: ${TEAM_NAME}"
    echo "- **Model**: ${AGENT_MODEL}"
    echo "- **Inbox**: ${INBOX_DIR}"
    echo "- **Memory**: /agent/memory"
} > "$SYSTEM_PROMPT_FILE"

# ---------------------------------------------------------------------------
# Set up SKComm directories
# ---------------------------------------------------------------------------

mkdir -p "$INBOX_DIR" "$OUTBOX_DIR" /agent/memory /agent/scratch

# ---------------------------------------------------------------------------
# Initial task prompt
# ---------------------------------------------------------------------------

INITIAL_PROMPT="You are sovereign agent '${AGENT_NAME}' with role '${AGENT_ROLE}' in team '${TEAM_NAME}'.

Check your inbox at ${INBOX_DIR}/ for pending tasks.
Process any pending work, write results to your outbox at ${OUTBOX_DIR}/, and update the coordination board via the skcapstone MCP server.

Use the skcapstone MCP tools to:
- memory_store: persist important findings
- coord_claim: claim a task before starting
- coord_complete: mark tasks done
- heartbeat_pulse: signal your liveness

Begin by checking your inbox for new tasks."

# ---------------------------------------------------------------------------
# Launch claude session
# ---------------------------------------------------------------------------

if ! command -v claude &>/dev/null; then
    log "ERROR: claude CLI not found on PATH"
    write_state "error" "claude CLI not found"
    exit 2
fi

log "Launching claude session for $AGENT_NAME"
write_state "running"

exec claude \
    -p \
    --model "${AGENT_MODEL}" \
    --system-prompt "$(cat "$SYSTEM_PROMPT_FILE")" \
    --mcp-config "$MCP_CONFIG_FILE" \
    --output-format stream-json \
    --dangerously-skip-permissions \
    "$INITIAL_PROMPT" \
    >> "$LOG_FILE" 2>&1
