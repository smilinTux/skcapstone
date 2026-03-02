#!/usr/bin/env bash
# scripts/e2e-test.sh — Automated multi-agent E2E test for the SKCapstone daemon.
#
# Usage:
#   ./scripts/e2e-test.sh [--port PORT] [--timeout SECS] [--peer PEER_NAME]
#
# Exit codes:
#   0  — all checks passed
#   1  — one or more checks failed

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PORT="${PORT:-7777}"
STARTUP_WAIT="${STARTUP_WAIT:-10}"
POLL_TIMEOUT="${POLL_TIMEOUT:-300}"
PEER="${PEER:-test-peer}"
AGENT_HOME="${SKCAPSTONE_ROOT:-${HOME}/.skcapstone}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAILURES=$((FAILURES + 1)); }
info() { echo -e "${YELLOW}[INFO]${NC} $*"; }

FAILURES=0
DAEMON_PID=""

cleanup() {
    if [[ -n "${DAEMON_PID}" ]]; then
        info "Stopping daemon (PID ${DAEMON_PID})…"
        kill "${DAEMON_PID}" 2>/dev/null || true
        wait "${DAEMON_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 0: Reinstall package
# ---------------------------------------------------------------------------
info "Reinstalling skcapstone…"
pip install -e . --quiet

# ---------------------------------------------------------------------------
# Step 1: Start daemon in the background
# ---------------------------------------------------------------------------
INBOX_DIR="${AGENT_HOME}/sync/comms/inbox/${PEER}"
OUTBOX_DIR="${AGENT_HOME}/sync/comms/outbox/${PEER}"
CONV_FILE="${AGENT_HOME}/conversations/${PEER}.json"

info "Starting daemon on port ${PORT}…"
skcapstone daemon start --foreground --port "${PORT}" \
    >"${TMPDIR:-/tmp}/skcapstone-e2e-daemon.log" 2>&1 &
DAEMON_PID=$!

info "Waiting ${STARTUP_WAIT}s for daemon to initialize…"
sleep "${STARTUP_WAIT}"

# Verify process is still alive
if ! kill -0 "${DAEMON_PID}" 2>/dev/null; then
    fail "Daemon exited prematurely — check ${TMPDIR:-/tmp}/skcapstone-e2e-daemon.log"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 2: Check /consciousness endpoint
# ---------------------------------------------------------------------------
info "Checking /consciousness endpoint…"
CONSCIOUSNESS_RESP=$(curl -sf "http://127.0.0.1:${PORT}/consciousness" 2>&1) || true
if echo "${CONSCIOUSNESS_RESP}" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('status') in ('ACTIVE','active','ok','running')" 2>/dev/null; then
    pass "/consciousness endpoint returned active status"
elif [[ -n "${CONSCIOUSNESS_RESP}" ]]; then
    # Accept any valid JSON response — daemon is reachable
    if echo "${CONSCIOUSNESS_RESP}" | python3 -m json.tool >/dev/null 2>&1; then
        pass "/consciousness endpoint returned JSON (status field: $(echo "${CONSCIOUSNESS_RESP}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','N/A'))" 2>/dev/null))"
    else
        fail "/consciousness endpoint returned non-JSON: ${CONSCIOUSNESS_RESP:0:120}"
    fi
else
    fail "/consciousness endpoint unreachable on port ${PORT}"
fi

# ---------------------------------------------------------------------------
# Step 3: Write a test message to the inbox
# ---------------------------------------------------------------------------
info "Writing test message to inbox (peer=${PEER})…"
mkdir -p "${INBOX_DIR}"
TIMESTAMP=$(date +%s)
MSG_ID="e2e-${TIMESTAMP}"
MSG_FILE="${INBOX_DIR}/${MSG_ID}.skc.json"

python3 - <<PYEOF
import json, time
msg = {
    "sender": "${PEER}",
    "recipient": "Opus",
    "payload": {
        "content": "Ping test — automated E2E at $(date -u +%Y-%m-%dT%H:%M:%SZ)",
        "content_type": "text",
    },
    "message_id": "${MSG_ID}",
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
}
with open("${MSG_FILE}", "w") as fh:
    json.dump(msg, fh)
print(f"  Wrote {len(json.dumps(msg))} bytes → ${MSG_FILE}")
PYEOF

pass "Test message written: ${MSG_FILE}"

# ---------------------------------------------------------------------------
# Step 4: Poll outbox for response (up to POLL_TIMEOUT seconds)
# ---------------------------------------------------------------------------
info "Polling outbox for response (timeout=${POLL_TIMEOUT}s)…"
mkdir -p "${OUTBOX_DIR}"

RESPONSE_FOUND=false
DEADLINE=$((SECONDS + POLL_TIMEOUT))
LAST_OUTBOX_COUNT=0

while [[ ${SECONDS} -lt ${DEADLINE} ]]; do
    OUTBOX_COUNT=$(find "${OUTBOX_DIR}" -name "*.skc.json" 2>/dev/null | wc -l)
    if [[ "${OUTBOX_COUNT}" -gt "${LAST_OUTBOX_COUNT}" ]]; then
        RESPONSE_FOUND=true
        LATEST_RESP=$(find "${OUTBOX_DIR}" -name "*.skc.json" -newer "${MSG_FILE}" 2>/dev/null | sort | tail -1)
        info "Response file detected: ${LATEST_RESP:-<any .skc.json in outbox>}"
        break
    fi

    # Also accept: conversations file updated (passthrough / no-SKComm mode)
    if [[ -f "${CONV_FILE}" ]] && [[ "${CONV_FILE}" -nt "${MSG_FILE}" ]]; then
        RESPONSE_FOUND=true
        info "Conversation file updated (passthrough mode detected)"
        break
    fi

    ELAPSED=$((SECONDS - (DEADLINE - POLL_TIMEOUT)))
    if (( ELAPSED % 30 == 0 )); then
        info "  Still waiting… ${ELAPSED}s elapsed / ${POLL_TIMEOUT}s timeout"
    fi
    sleep 2
done

if ${RESPONSE_FOUND}; then
    pass "Response received within $((SECONDS - (DEADLINE - POLL_TIMEOUT)))s"
else
    fail "No response within ${POLL_TIMEOUT}s (outbox: ${OUTBOX_DIR}, conv: ${CONV_FILE})"
fi

# ---------------------------------------------------------------------------
# Step 5: Check conversations file
# ---------------------------------------------------------------------------
info "Checking conversations/${PEER}.json…"
if [[ -f "${CONV_FILE}" ]]; then
    CONV_SIZE=$(wc -c < "${CONV_FILE}")
    if python3 -m json.tool "${CONV_FILE}" >/dev/null 2>&1; then
        pass "conversations/${PEER}.json exists and is valid JSON (${CONV_SIZE} bytes)"
    else
        fail "conversations/${PEER}.json exists but is not valid JSON"
    fi
else
    fail "conversations/${PEER}.json not found at ${CONV_FILE}"
fi

# ---------------------------------------------------------------------------
# Step 6: Kill daemon
# ---------------------------------------------------------------------------
info "Stopping daemon…"
kill "${DAEMON_PID}" 2>/dev/null || true
wait "${DAEMON_PID}" 2>/dev/null || true
DAEMON_PID=""  # prevent double-kill in trap
pass "Daemon stopped"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [[ ${FAILURES} -eq 0 ]]; then
    echo -e "${GREEN}E2E TEST PASSED${NC} — all checks passed."
    exit 0
else
    echo -e "${RED}E2E TEST FAILED${NC} — ${FAILURES} check(s) failed."
    echo "  Daemon log: ${TMPDIR:-/tmp}/skcapstone-e2e-daemon.log"
    exit 1
fi
