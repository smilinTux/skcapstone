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

# Initial sync on startup
log "Starting token watcher..."
sync_token

# Watch for changes to credentials file
log "Watching $CREDS for changes..."
while true; do
    # inotifywait blocks until the file is modified, then we sync
    inotifywait -q -e modify -e close_write -e moved_to "$(dirname "$CREDS")" --include "$(basename "$CREDS")" 2>/dev/null || {
        # If inotifywait isn't available, fall back to polling every 30 seconds
        log "WARN: inotifywait not available, falling back to 30s polling"
        while true; do
            sleep 30
            sync_token
        done
    }
    # Small delay to let Claude Code finish writing
    sleep 2
    sync_token
done
