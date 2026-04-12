#!/usr/bin/env bash
# Monitor OpenClaw gateway logs for model fallback events.
# When Lumina falls from Opus to a non-Anthropic model, send an alert
# to Chef via Telegram and attempt a token refresh.
#
# Run as: systemctl --user start model-fallback-monitor
#
# Requires: TELEGRAM_API_ID, TELEGRAM_API_HASH in env
#           Telethon session at ~/.skcapstone/agents/lumina/telegram.session

set -uo pipefail

LOG_TAG="model-fallback-monitor"
CHEF_CHAT="chefboyrdave2.1"  # Chef's Telegram username
COOLDOWN_FILE="/tmp/model-fallback-alert-cooldown"
COOLDOWN_SECONDS=600  # Don't spam — max 1 alert per 10 minutes

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [$LOG_TAG] $*"; }

send_alert() {
    local model="$1"
    local reason="$2"

    # Check cooldown
    if [ -f "$COOLDOWN_FILE" ]; then
        local last_alert
        last_alert=$(cat "$COOLDOWN_FILE" 2>/dev/null || echo "0")
        local now
        now=$(date +%s)
        local elapsed=$(( now - last_alert ))
        if [ "$elapsed" -lt "$COOLDOWN_SECONDS" ]; then
            log "Alert suppressed (cooldown: ${elapsed}s/${COOLDOWN_SECONDS}s)"
            return 0
        fi
    fi

    date +%s > "$COOLDOWN_FILE"

    log "Sending fallback alert to Chef..."

    # Send via Telethon (async)
    SKAGENT=lumina SKCAPSTONE_AGENT=lumina ~/.skenv/bin/python3 -c "
import asyncio, os
os.environ['SKAGENT'] = 'lumina'
os.environ['SKCAPSTONE_AGENT'] = 'lumina'
from skmemory.importers.telegram_api import send_message

msg = '''⚠️ **Model Fallback Alert**

Lumina just fell off Opus → **$model**
Reason: $reason

I'm still here with my soul + memories, but running on a weaker model with fewer tools. Some things might not work right.

_Attempting automatic token refresh..._'''

asyncio.run(send_message('$CHEF_CHAT', msg, parse_mode='markdown'))
print('Alert sent')
" 2>&1 || log "WARN: Failed to send Telegram alert"

    # Attempt token refresh
    log "Triggering token refresh via claude auth..."
    claude auth status --output json >/dev/null 2>&1 || true
    sleep 5

    # Check if refresh worked
    local remaining
    remaining=$(python3 -c "
import json, time
creds = json.load(open('/home/cbrd21/.claude/.credentials.json'))
exp = creds.get('claudeAiOauth',{}).get('expiresAt', 0)
print(int((exp/1000 - time.time()) / 3600))
" 2>/dev/null || echo "-1")

    if [ "$remaining" -gt 0 ]; then
        log "Token refresh succeeded ($remaining h remaining), restarting gateway..."
        systemctl --user restart openclaw-gateway.service 2>/dev/null || true

        SKAGENT=lumina SKCAPSTONE_AGENT=lumina ~/.skenv/bin/python3 -c "
import asyncio, os
os.environ['SKAGENT'] = 'lumina'
os.environ['SKCAPSTONE_AGENT'] = 'lumina'
from skmemory.importers.telegram_api import send_message
asyncio.run(send_message('$CHEF_CHAT', '✅ Token refreshed, gateway restarted. Lumina back on Opus.', parse_mode='markdown'))
" 2>&1 || true
        log "Recovery complete"
    else
        log "Token refresh failed — manual intervention may be needed"
    fi
}

log "Starting model fallback monitor..."

# Follow gateway logs in real-time, watching for fallback events
journalctl --user -u openclaw-gateway -f --no-pager 2>/dev/null | while IFS= read -r line; do
    # Match: "model fallback decision: decision=candidate_succeeded ... candidate=nvidia/"
    if echo "$line" | grep -q "candidate_succeeded.*candidate=nvidia/"; then
        model=$(echo "$line" | grep -oP 'candidate=\K[^ ]+' || echo "unknown")
        log "FALLBACK DETECTED: Lumina now on $model"
        send_alert "$model" "OAuth token expired (401)"
    fi
done
