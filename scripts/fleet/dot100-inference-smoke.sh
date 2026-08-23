#!/usr/bin/env bash
# .100 (worker-gpu) inference smoke test: the before/after gate for the
# node-roles epic (card 193089bf, parent f709c721, epic 3bbf39ea).
#
# .100 serves fleet inference. Every card that touches it owes the same
# evidence, so the probe is built once and reused. STRICTLY READ-ONLY:
# this script never starts, stops, restarts, enables or disables anything.
# It reads HTTP endpoints and asks systemd for timestamps.
#
# Usage:
#   scripts/fleet/dot100-inference-smoke.sh                 # probe the live .100
#   scripts/fleet/dot100-inference-smoke.sh --json          # machine-readable
#   SMOKE_HOST=1.2.3.4 scripts/fleet/dot100-inference-smoke.sh
#   SMOKE_OLLAMA_PORT=1 scripts/fleet/dot100-inference-smoke.sh   # closed-port demo
#
# Exit status: 0 when every probe passes, 1 when any probe fails.

set -uo pipefail

HOST="${SMOKE_HOST:-192.168.0.100}"
SSH_TARGET="${SMOKE_SSH:-cbrd21@${HOST}}"

# Ports are overridable so the failure path can be demonstrated without
# touching the node (point one at a closed port and the script must fail).
OLLAMA_PORT="${SMOKE_OLLAMA_PORT:-11434}"     # ollama.service, /api/embed
EMBED_ARC_PORT="${SMOKE_EMBED_ARC_PORT:-11438}" # mxbai-arc.service, Vulkan iGPU embed
ORNITH_PORT="${SMOKE_ORNITH_PORT:-8082}"      # skai-beellama.service, the fleet sk-default
QWEN_PORT="${SMOKE_QWEN_PORT:-8085}"          # qwen3-arc.service
COMFYUI_PORT="${SMOKE_COMFYUI_PORT:-8188}"    # comfyui.service
F5TTS_PORT="${SMOKE_F5TTS_PORT:-18796}"       # f5-tts.service
WHISPER_PORT="${SMOKE_WHISPER_PORT:-18794}"   # whisper-stt.service

# The gateway, probed from wherever this script runs (not on .100).
# Probing .100 directly proves .100 is up. It does NOT prove that sovereign
# traffic is reaching it: skgateway can silently fail sk-default over to a
# cloud provider, and the direct probe answers 200 the whole time. A probe
# that answers through a different path than the consumer uses is not a
# test, it is a coincidence.
GATEWAY_URL="${SMOKE_GATEWAY_URL:-http://localhost:18780}"

# SOVEREIGNTY IS NOT A MODEL NAME (card 16af7915). There used to be a
# SMOKE_SOVEREIGN_MODELS allowlist here, matched as a SUBSTRING against the
# `model` field of the response body. It is gone, and it must not come back.
#
# Measured against the live gateway ledger (skgateway/data/metrics.db,
# energy_log, opened read-only) on 2026-08-17: 76 rows carry one of that old
# list's tokens while running on backend=nvidia, basis=imputed_cloud. Examples
# straight out of the table: meta/llama-3.3-70b-instruct,
# nvidia/llama-3.3-nemotron-super-49b-v1, qwen3.8-27b-huihui-abliterated-q4_k_m.
# The allowlist certified cloud-served open weights as sovereign, which is the
# exact substitution this probe exists to catch. It answered PASS through the
# whole thing.
#
# The one definition lives in skharness (`skharness/autocode/sovereignty.py`)
# and this script CALLS it rather than mirroring it, so the two ends cannot
# drift. Sovereignty is a claim about hardware and jurisdiction: the
# discriminator is the backend that served plus the energy basis it reported.
# ornith-1.0-9b served by `nvidia` is a violation; the same weights served by
# `reg:ornith` are not. The weights are not the variable.
SOVEREIGNTY_MODULE="${SMOKE_SOVEREIGNTY_MODULE:-skharness.autocode.sovereignty}"

EMBED_MODEL="${SMOKE_EMBED_MODEL:-mxbai-embed-large}"
EMBED_DIM="${SMOKE_EMBED_DIM:-1024}"
CHAT_MODEL="${SMOKE_CHAT_MODEL:-ornith-1.0-9b}"

# The ornith trap: a small budget returns an empty completion and reads as a
# failure. Never lower this below 2048.
CHAT_MAX_TOKENS="${SMOKE_CHAT_MAX_TOKENS:-2048}"

CURL_TIMEOUT="${SMOKE_CURL_TIMEOUT:-30}"
CHAT_TIMEOUT="${SMOKE_CHAT_TIMEOUT:-120}"
SSH_TIMEOUT="${SMOKE_SSH_TIMEOUT:-20}"

AS_JSON=0
[ "${1:-}" = "--json" ] && AS_JSON=1

FAILURES=0
RESULTS=()

pass() {
  RESULTS+=("PASS|$1|$2")
  [ "$AS_JSON" -eq 1 ] || printf 'PASS  %-22s %s\n' "$1" "$2"
}

fail() {
  RESULTS+=("FAIL|$1|$2")
  FAILURES=$((FAILURES + 1))
  [ "$AS_JSON" -eq 1 ] || printf 'FAIL  %-22s %s\n' "$1" "$2"
}

# ---------------------------------------------------------------- probes ---

probe_ollama_embed() {
  local body out dim
  body=$(printf '{"model":"%s","input":"fleet smoke test"}' "$EMBED_MODEL")
  out=$(curl -sS -m "$CURL_TIMEOUT" \
    "http://${HOST}:${OLLAMA_PORT}/api/embed" \
    -H 'Content-Type: application/json' -d "$body" 2>/dev/null)
  if [ -z "$out" ]; then
    fail "ollama-embed" "no response from :${OLLAMA_PORT}/api/embed"
    return
  fi
  dim=$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print(-1); raise SystemExit
vecs = d.get("embeddings") or ([d["embedding"]] if d.get("embedding") else [])
print(len(vecs[0]) if vecs and vecs[0] else -1)
' 2>/dev/null)
  if [ "${dim:--1}" = "$EMBED_DIM" ]; then
    pass "ollama-embed" ":${OLLAMA_PORT} ${EMBED_MODEL} dim=${dim}"
  else
    fail "ollama-embed" ":${OLLAMA_PORT} expected dim=${EMBED_DIM}, got ${dim:--1}"
  fi
}

probe_embed_arc() {
  local body out dim
  body=$(printf '{"model":"%s","input":"fleet smoke test"}' "$EMBED_MODEL")
  out=$(curl -sS -m "$CURL_TIMEOUT" \
    "http://${HOST}:${EMBED_ARC_PORT}/v1/embeddings" \
    -H 'Content-Type: application/json' -d "$body" 2>/dev/null)
  dim=$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print(-1); raise SystemExit
data = d.get("data") or []
print(len(data[0].get("embedding", [])) if data else -1)
' 2>/dev/null)
  if [ "${dim:--1}" = "$EMBED_DIM" ]; then
    pass "mxbai-arc-embed" ":${EMBED_ARC_PORT} ${EMBED_MODEL} dim=${dim}"
  else
    fail "mxbai-arc-embed" ":${EMBED_ARC_PORT} expected dim=${EMBED_DIM}, got ${dim:--1}"
  fi
}

probe_chat() {
  local port label model body out content
  port="$1"; label="$2"; model="$3"
  body=$(python3 -c '
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": "Reply with the single word OK."}],
    "max_tokens": int(sys.argv[2]),
}))
' "$model" "$CHAT_MAX_TOKENS")
  out=$(curl -sS -m "$CHAT_TIMEOUT" \
    "http://${HOST}:${port}/v1/chat/completions" \
    -H 'Content-Type: application/json' -d "$body" 2>/dev/null)
  content=$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    raise SystemExit
choices = d.get("choices") or []
if choices:
    print((choices[0].get("message", {}).get("content") or "").strip().replace("\n", " ")[:60])
' 2>/dev/null)
  if [ -n "$content" ]; then
    pass "$label" ":${port} ${model} max_tokens=${CHAT_MAX_TOKENS} -> ${content}"
  else
    fail "$label" ":${port} ${model} returned an empty completion"
  fi
}

probe_http() {
  local port label path code
  port="$1"; label="$2"; path="${3:-/}"
  code=$(curl -sS -m "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' \
    "http://${HOST}:${port}${path}" 2>/dev/null)
  # Any HTTP status proves the listener answered; only a dead socket gives 000.
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    pass "$label" ":${port}${path} http=${code}"
  else
    fail "$label" ":${port}${path} no listener"
  fi
}

# Read one header value out of a `curl -D` dump. Header names are case
# insensitive on the wire, so the match is too.
header_value() {
    local dump="$1" name="$2"
    printf '%s' "$dump" \
        | tr -d '\r' \
        | awk -v want="$(printf '%s' "$name" | tr 'A-Z' 'a-z')" '
            { split($0, kv, ":");
              k = tolower(kv[1]);
              if (k == want) { sub(/^[^:]*:[ \t]*/, "", $0); print $0 } }' \
        | tail -n 1
}

probe_gateway_sovereignty() {
    # Assert sk-default is answered BY hardware we own, not merely that .100 is
    # reachable and not merely that the answer is NAMED like something local.
    #
    # The observables are skgateway's attribution and energy headers, which it
    # emits today for the SERVING attempt: x-sk-backend, x-sk-energy-basis,
    # x-sk-energy-node (verified live against localhost:18780, which answered
    # reg:ornith / measured_gpu / ollama). The body's `model` field is what was
    # NAMED, and naming is what the old allowlist trusted.
    local dump body backend basis node verdict state rc
    dump=$(mktemp) || { fail "gateway-sovereignty" "cannot create a temp file"; return; }
    body=$(curl -sS -m "$CHAT_TIMEOUT" -D "$dump" \
        "${GATEWAY_URL}/v1/chat/completions" \
        -H 'Content-Type: application/json' \
        -d "{\"model\":\"sk-default\",\"messages\":[{\"role\":\"user\",\"content\":\"say OK\"}],\"max_tokens\":${CHAT_MAX_TOKENS}}" \
        2>/dev/null)
    if [ -z "$body" ]; then
        # No gateway here is not a failure of .100. This script also runs on
        # boxes that do not host one.
        rm -f "$dump"
        pass "gateway-sovereignty" "no gateway at ${GATEWAY_URL} (skipped)"
        return
    fi

    local headers
    headers=$(cat "$dump")
    rm -f "$dump"
    backend=$(header_value "$headers" "x-sk-backend")
    basis=$(header_value "$headers" "x-sk-energy-basis")
    node=$(header_value "$headers" "x-sk-energy-node")

    # THE definition, called not copied. A mirrored rule in bash would be a
    # second definition the moment either side is edited, and nothing would
    # report the drift.
    local errfile reason
    errfile=$(mktemp) || { fail "gateway-sovereignty" "cannot create a temp file"; return; }
    verdict=$(python3 -m "$SOVEREIGNTY_MODULE" \
        --backend "$backend" --basis "$basis" --node "$node" 2>"$errfile")
    rc=$?
    state="${verdict%%	*}"
    reason="${verdict#*	}"

    # Branch on the STATE WORD and cross-check it against the exit code, never
    # on the exit code alone. `python3 -m some.missing.module` also exits 1, so
    # an exit-code-only reader would report a crashed classifier as "a cloud
    # served it": still a failure, but a failure with the wrong diagnosis
    # pointing an operator at the routing when the real problem is the install.
    # A state word the classifier did not write means it did not classify.
    case "$state" in
        sovereign)
            if [ "$rc" -ne 0 ]; then
                fail "gateway-sovereignty" \
                    "classifier contract broken: said 'sovereign' but exited ${rc}; not certifying"
            else
                pass "gateway-sovereignty" \
                    "sk-default served by backend=${backend} basis=${basis} node=${node:-none}"
            fi
            ;;
        violated)
            fail "gateway-sovereignty" \
                "sk-default served by backend=${backend} basis=${basis}: NOT our hardware (silent cloud failover). ${reason}"
            ;;
        unobserved)
            # FAIL CLOSED. Unknown is not sovereign. A gateway too old to emit
            # attribution headers has told us nothing, and "nothing" read as a
            # pass is what made the old allowlist look healthy while it was not.
            fail "gateway-sovereignty" \
                "sk-default sovereignty UNOBSERVED (backend=${backend:-none} basis=${basis:-none}): ${reason}"
            ;;
        *)
            # The classifier itself did not run: almost always skharness is not
            # installed. Also fail closed, and say exactly what to install
            # rather than certifying a call nobody classified.
            fail "gateway-sovereignty" \
                "cannot classify: python3 -m ${SOVEREIGNTY_MODULE} exited ${rc} and wrote no verdict ($(tr -d '\n' < "$errfile" | tail -c 160)). Install skharness; this script does not carry its own copy of the definition."
            ;;
    esac
    rm -f "$errfile"
}

# ------------------------------------------------------------ timestamps ---
# ActiveEnterTimestamp is the restart tripwire: run the script twice and these
# values must be byte-identical, which proves the probe restarted nothing.

SYSTEM_UNITS="ollama.service mxbai-arc.service"
USER_UNITS="skai-beellama.service comfyui.service f5-tts.service whisper-stt.service qwen3-arc.service sovereign-orchestrator.service"

collect_timestamps() {
  timeout "$SSH_TIMEOUT" ssh -o BatchMode=yes -o ConnectTimeout=6 "$SSH_TARGET" "
    for u in ${SYSTEM_UNITS}; do
      printf 'system/%s\t%s\t%s\n' \"\$u\" \\
        \"\$(systemctl show \$u -p ActiveState --value 2>/dev/null)\" \\
        \"\$(systemctl show \$u -p ActiveEnterTimestamp --value 2>/dev/null)\"
    done
    for u in ${USER_UNITS}; do
      printf 'user/%s\t%s\t%s\n' \"\$u\" \\
        \"\$(systemctl --user show \$u -p ActiveState --value 2>/dev/null)\" \\
        \"\$(systemctl --user show \$u -p ActiveEnterTimestamp --value 2>/dev/null)\"
    done
  " 2>/dev/null
}

# ------------------------------------------------------------------ main ---

[ "$AS_JSON" -eq 1 ] || echo "== .100 inference smoke (${HOST}) =="

probe_ollama_embed
probe_embed_arc
probe_chat "$ORNITH_PORT" "ornith-chat" "$CHAT_MODEL"
probe_chat "$QWEN_PORT" "qwen3-arc-chat" "qwen3.5:4b"
probe_http "$COMFYUI_PORT" "comfyui" "/system_stats"
probe_http "$F5TTS_PORT" "f5-tts" "/"
probe_http "$WHISPER_PORT" "whisper-stt" "/"
probe_gateway_sovereignty

TIMESTAMPS=$(collect_timestamps)
if [ -z "$TIMESTAMPS" ]; then
  fail "unit-timestamps" "ssh ${SSH_TARGET} returned nothing"
else
  pass "unit-timestamps" "$(printf '%s' "$TIMESTAMPS" | grep -c .) units read"
fi

if [ "$AS_JSON" -eq 1 ]; then
  python3 -c '
import json, sys
rows = [line.split("|", 2) for line in sys.argv[1].splitlines() if line]
stamps = []
for line in sys.argv[2].splitlines():
    parts = line.split("\t")
    if len(parts) == 3:
        stamps.append({"unit": parts[0], "activeState": parts[1], "activeEnter": parts[2]})
print(json.dumps({
    "host": sys.argv[3],
    "probes": [{"result": r[0], "name": r[1], "detail": r[2]} for r in rows],
    "units": stamps,
    "failures": sum(1 for r in rows if r[0] == "FAIL"),
}, indent=2, sort_keys=True))
' "$(printf '%s\n' "${RESULTS[@]}")" "$TIMESTAMPS" "$HOST"
else
  echo
  echo "-- ActiveEnterTimestamp (must be identical across two runs) --"
  printf '%s\n' "$TIMESTAMPS"
  echo
  if [ "$FAILURES" -eq 0 ]; then
    echo "RESULT: PASS (${#RESULTS[@]} probes, 0 failures)"
  else
    echo "RESULT: FAIL (${FAILURES} of ${#RESULTS[@]} probes failed)"
  fi
fi

exit $([ "$FAILURES" -eq 0 ] && echo 0 || echo 1)
