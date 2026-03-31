#!/usr/bin/env bash
# Watch Claude Code credentials file and sync token to OpenClaw immediately on change.
#
# Replaces the 2-hour timer approach which missed token refreshes.
# Claude Code auto-refreshes its OAuth token and writes to .credentials.json.
# This script detects the write and syncs within seconds.
#
# Requires: inotifywait (from inotify-tools package)
# Install: sudo apt install inotify-tools
#
# Run as systemd user service (not timer).

set -euo pipefail

CREDS="$HOME/.claude/.credentials.json"
OPENCLAW_JSON="$HOME/.openclaw/openclaw.json"
OPENCLAW_ENV="$HOME/.openclaw/.env"
OVERRIDE_CONF="$HOME/.config/systemd/user/openclaw-gateway.service.d/override.conf"
LOG_TAG="anthropic-token-watch"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [$LOG_TAG] $*"; }

sync_token() {
    if [ ! -f "$CREDS" ]; then
        log "ERROR: Claude credentials not found at $CREDS"
        return 1
    fi

    # Read current token from Claude Code
    local new_token
    new_token=$(python3 -c "import json; print(json.load(open('$CREDS'))['claudeAiOauth']['accessToken'])" 2>/dev/null) || {
        log "ERROR: Failed to read token from credentials"
        return 1
    }

    local expires_in
    expires_in=$(python3 -c "import json,time; print(f'{(json.load(open(\"$CREDS\"))[\"claudeAiOauth\"][\"expiresAt\"]/1000 - time.time())/3600:.1f}h')" 2>/dev/null || echo "unknown")

    # Read current token from OpenClaw
    local current_token
    current_token=$(python3 -c "import json; print(json.load(open('$OPENCLAW_JSON'))['models']['providers']['anthropic']['apiKey'])" 2>/dev/null || echo "")

    if [ "$new_token" = "$current_token" ]; then
        log "Token unchanged (expires in $expires_in)"
        return 0
    fi

    log "Token changed! Syncing... (new token expires in $expires_in)"

    # 1. Update openclaw.json
    python3 << PYEOF
import json
with open('$OPENCLAW_JSON') as f:
    cfg = json.load(f)
if 'anthropic' in cfg.get('models', {}).get('providers', {}):
    cfg['models']['providers']['anthropic']['apiKey'] = '$new_token'
    with open('$OPENCLAW_JSON', 'w') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
PYEOF
    log "Updated openclaw.json"

    # 2. Update .env
    if grep -q "^ANTHROPIC_API_KEY=" "$OPENCLAW_ENV" 2>/dev/null; then
        sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$new_token|" "$OPENCLAW_ENV"
    else
        echo "ANTHROPIC_API_KEY=$new_token" >> "$OPENCLAW_ENV"
    fi
    log "Updated .env"

    # 3. Update systemd override
    if [ -f "$OVERRIDE_CONF" ]; then
        local nvidia_key
        nvidia_key=$(grep "NVIDIA_API_KEY=" "$OVERRIDE_CONF" 2>/dev/null | sed 's/.*NVIDIA_API_KEY=//' || true)
        cat > "$OVERRIDE_CONF" << EOF
[Unit]
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
RestartSec=10
Environment=NVIDIA_API_KEY=${nvidia_key}
Environment=ANTHROPIC_API_KEY=${new_token}
EOF
        log "Updated systemd override"
    fi

    # 4. Reload and restart gateway
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user restart openclaw-gateway 2>/dev/null && log "Gateway restarted" || log "WARN: Gateway restart failed (may not be running as systemd service)"

    # 5. Sync credentials to GPU box (.100) for skvoice service
    scp -q "$CREDS" cbrd21@192.168.0.100:~/.claude/.credentials.json 2>/dev/null && log "Synced credentials to .100" || log "WARN: Failed to sync credentials to .100"

    log "Sync complete. Token expires in $expires_in"
}

# Proactive token refresh — refresh before expiry even if no Claude Code session is running
refresh_token_proactively() {
    if [ ! -f "$CREDS" ]; then return 0; fi

    local remaining_ms
    remaining_ms=$(python3 -c "
import json, time
creds = json.load(open('$CREDS'))
exp = creds.get('claudeAiOauth',{}).get('expiresAt', 0)
print(int(exp - time.time() * 1000))
" 2>/dev/null || echo "999999999")

    # Refresh if less than 3 hours remaining (10800000 ms) — gives time for retries before expiry
    if [ "$remaining_ms" -gt 10800000 ]; then
        local remaining_h=$(( remaining_ms / 3600000 ))
        log "Token still valid (${remaining_h}h remaining), no refresh needed"
        return 0
    fi

    log "Token expiring/expired (${remaining_ms}ms remaining) — proactively refreshing..."

    # Strategy: use `claude auth status` to trigger Claude Code's built-in
    # token refresh. This is far more reliable than calling the OAuth endpoint
    # ourselves (which gets 429 rate-limited every time).
    # Claude Code manages its own PKCE state, session cookies, etc. — just let it.
    local MAX_RETRIES=3
    local attempt=0
    local refreshed=false

    while [ "$attempt" -lt "$MAX_RETRIES" ]; do
        attempt=$((attempt + 1))
        log "Refresh attempt $attempt/$MAX_RETRIES via 'claude auth status'..."

        # claude auth status checks credentials and refreshes if needed
        # --output json ensures clean non-interactive output
        local output
        output=$(claude auth status --output json 2>&1) || true

        # Check if the token was actually refreshed (file mtime changed)
        local new_remaining_ms
        new_remaining_ms=$(python3 -c "
import json, time
creds = json.load(open('$CREDS'))
exp = creds.get('claudeAiOauth',{}).get('expiresAt', 0)
print(int(exp - time.time() * 1000))
" 2>/dev/null || echo "0")

        if [ "$new_remaining_ms" -gt 10800000 ]; then
            local new_h=$(( new_remaining_ms / 3600000 ))
            log "Token refreshed successfully (${new_h}h remaining)"
            refreshed=true
            break
        fi

        log "Token still expired after attempt $attempt, waiting 30s..."
        sleep 30
    done

    if [ "$refreshed" = "false" ]; then
        log "ERROR: All $MAX_RETRIES refresh attempts via claude auth failed"
        log "Token may require manual 'claude auth login' to re-authenticate"
    fi

    local rc=$?
    if [ "$rc" -eq 0 ]; then
        log "Proactive refresh succeeded"
        # sync_token will fire from the inotifywait detecting the file write,
        # but also call it directly in case inotifywait misses the self-write
        sync_token
    else
        log "ERROR: Proactive refresh failed (rc=$rc)"
    fi
    return 0  # Never let refresh failure kill the watcher loop
}

# Compute inotifywait timeout based on token remaining life.
# When token is healthy: check every 30m. Near expiry (<2h): check every 5m.
# Already expired: check every 2m (retry window for 429 backoff).
get_watch_timeout() {
    local remaining_ms
    remaining_ms=$(python3 -c "
import json, time
creds = json.load(open('$CREDS'))
exp = creds.get('claudeAiOauth',{}).get('expiresAt', 0)
print(int(exp - time.time() * 1000))
" 2>/dev/null || echo "0")

    if [ "$remaining_ms" -le 0 ]; then
        echo 120    # Expired: retry every 2 minutes
    elif [ "$remaining_ms" -le 10800000 ]; then
        echo 180    # <3h remaining: check every 3 minutes
    else
        echo 1800   # Healthy: check every 30 minutes
    fi
}

# Initial sync on startup — also refresh proactively if token is expired/expiring
log "Starting token watcher..."
sync_token || true
refresh_token_proactively || true

# Watch for changes to credentials file + proactive refresh timer
log "Watching $CREDS for changes (with adaptive refresh interval)..."
while true; do
    timeout=$(get_watch_timeout)
    # inotifywait returns: 0=event, 1=error, 2=timeout
    # CRITICAL: use `|| true` to prevent set -e from killing the script on timeout
    inotifywait -q -t "$timeout" -e modify -e close_write -e moved_to \
        "$(dirname "$CREDS")" --include "$(basename "$CREDS")" 2>/dev/null || true

    # Always check for proactive refresh on every loop iteration
    # This handles both timeout and file-change cases
    refresh_token_proactively || true

    # If file was modified externally (Claude Code session), also sync
    if [ -f "$CREDS" ]; then
        sleep 1
        sync_token || true
    fi
done
