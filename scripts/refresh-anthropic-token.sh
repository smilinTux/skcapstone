#!/usr/bin/env bash
# Proactive Anthropic OAuth token refresh + sync to OpenClaw gateway.
#
# Two-phase approach (prb-021b489e):
#   Phase 1: If token is expiring (<2h) or expired, refresh it:
#     a) Try `claude auth status` (lightweight, no interactive session)
#     b) If that fails, spin up ephemeral Claude Code in tmux → triggers internal refresh → kill it
#   Phase 2: Sync the (possibly refreshed) token to OpenClaw config + restart gateway if changed.
#
# Run via systemd timer every 4 hours.
set -euo pipefail

_sed_i() { if [[ "$OSTYPE" == "darwin"* ]]; then sed -i '' "$@"; else sed -i "$@"; fi; }

CREDS="$HOME/.claude/.credentials.json"
OPENCLAW_JSON="$HOME/.openclaw/openclaw.json"
OPENCLAW_ENV="$HOME/.openclaw/.env"
OVERRIDE_CONF="$HOME/.config/systemd/user/openclaw-gateway.service.d/override.conf"
LOG_TAG="anthropic-token-refresh"
TMUX_SESSION="token-refresh-ephemeral"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [$LOG_TAG] $*"; }

if [ ! -f "$CREDS" ]; then
    log "ERROR: Claude credentials not found at $CREDS"
    exit 1
fi

get_remaining_ms() {
    python3 -c "
import json, time
creds = json.load(open('$CREDS'))
exp = creds.get('claudeAiOauth',{}).get('expiresAt', 0)
print(int(exp - time.time() * 1000))
" 2>/dev/null || echo "0"
}

get_remaining_h() {
    python3 -c "
import json, time
creds = json.load(open('$CREDS'))
exp = creds.get('claudeAiOauth',{}).get('expiresAt', 0)
print(f'{(exp/1000 - time.time())/3600:.1f}')
" 2>/dev/null || echo "0"
}

token_needs_refresh() {
    local remaining_ms
    remaining_ms=$(get_remaining_ms)
    # Refresh if less than 4 hours remaining (was 2h — too tight with 3h timer)
    [ "$remaining_ms" -le 14400000 ]
}

token_is_healthy() {
    local remaining_ms
    remaining_ms=$(get_remaining_ms)
    [ "$remaining_ms" -gt 14400000 ]
}

# ─── Phase 1: Refresh token if needed ───────────────────────────────

if token_needs_refresh; then
    log "Token needs refresh ($(get_remaining_h)h remaining)"

    # Step 1a: Try lightweight refresh
    log "Attempting lightweight refresh via 'claude auth status'..."
    claude auth status > /dev/null 2>&1 || true
    sleep 2

    if token_is_healthy; then
        log "Lightweight refresh succeeded! ($(get_remaining_h)h remaining)"
    else
        # Step 1b: Ephemeral Claude Code session in tmux
        log "Lightweight refresh didn't cut it — spinning up ephemeral Claude Code session..."
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true

        tmux new-session -d -s "$TMUX_SESSION" \
            "claude -p 'respond with just OK' --output-format stream-json 2>/dev/null; exit"

        refreshed=false
        for i in $(seq 1 12); do
            sleep 5
            if token_is_healthy; then
                log "Ephemeral session refreshed the token! ($(get_remaining_h)h remaining)"
                refreshed=true
                break
            fi
        done

        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true

        if [ "$refreshed" = "false" ]; then
            log "ERROR: All refresh attempts failed ($(get_remaining_h)h remaining)"
            log "Manual intervention may be needed: claude auth login"
            # Continue to sync phase anyway — sync whatever token we have
        fi
    fi
else
    log "Token is healthy ($(get_remaining_h)h remaining), no refresh needed"
fi

# ─── Phase 2: Sync token to OpenClaw ────────────────────────────────

ACCESS_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS'))['claudeAiOauth']['accessToken'])")
REMAINING=$(get_remaining_h)

# Check what's currently in the systemd override
OLD_TOKEN=""
if [ -f "$OVERRIDE_CONF" ]; then
    OLD_TOKEN=$(grep "ANTHROPIC_API_KEY=" "$OVERRIDE_CONF" 2>/dev/null | sed 's/.*ANTHROPIC_API_KEY=//' || true)
fi

if [ "$OLD_TOKEN" = "$ACCESS_TOKEN" ]; then
    log "Token already synced (expires in ${REMAINING}h)"
    exit 0
fi

log "Token changed, syncing to OpenClaw..."

# 1. Update openclaw.json
if [ -f "$OPENCLAW_JSON" ]; then
    python3 -c "
import json
with open('$OPENCLAW_JSON') as f:
    cfg = json.load(f)
if 'providers' in cfg.get('models', {}):
    if 'anthropic' in cfg['models']['providers']:
        cfg['models']['providers']['anthropic']['apiKey'] = '$ACCESS_TOKEN'
        with open('$OPENCLAW_JSON', 'w') as f:
            json.dump(cfg, f, indent=2)
            f.write('\n')
        print('[sync] Updated openclaw.json')
    else:
        print('[sync] No anthropic provider in openclaw.json')
else:
    print('[sync] No providers section in openclaw.json')
"
fi

# 2. Update .env
if grep -q "^ANTHROPIC_API_KEY=" "$OPENCLAW_ENV" 2>/dev/null; then
    _sed_i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$ACCESS_TOKEN|" "$OPENCLAW_ENV"
else
    echo "ANTHROPIC_API_KEY=$ACCESS_TOKEN" >> "$OPENCLAW_ENV"
fi
log "Updated .env"

# 3. Update systemd override
NVIDIA_KEY=$(grep "NVIDIA_API_KEY=" "$OVERRIDE_CONF" 2>/dev/null | sed 's/.*NVIDIA_API_KEY=//' || true)
cat > "$OVERRIDE_CONF" << EOF
[Unit]
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
RestartSec=10
Environment=NVIDIA_API_KEY=${NVIDIA_KEY}
Environment=ANTHROPIC_API_KEY=${ACCESS_TOKEN}
EOF
log "Updated systemd override"

# 4. Reload systemd (for env vars) but DO NOT restart the gateway.
#    OpenClaw uses chokidar to watch openclaw.json — updating the file above
#    triggers a hot reload automatically.  Restarting the gateway kills all
#    active sessions (the root cause of the 0-turn session cascade on 2026-04-07).
systemctl --user daemon-reload

# Touch the config to ensure chokidar picks up the change (write already did,
# but belt-and-suspenders in case the mtime didn't change fast enough).
touch "$OPENCLAW_JSON"

log "Token synced via hot reload (expires in ${REMAINING}h) — gateway NOT restarted, active sessions preserved"
