#!/usr/bin/env bash
# Sync Anthropic OAuth token from Claude Code credentials to OpenClaw gateway
#
# Claude Code manages its own token refresh internally (writing to .credentials.json).
# This script simply reads the current token and syncs it to:
#   1. ~/.openclaw/openclaw.json (anthropic provider apiKey)
#   2. ~/.openclaw/.env (ANTHROPIC_API_KEY)
#   3. systemd override (ANTHROPIC_API_KEY env var)
# Then restarts the gateway if the token changed.
#
# Run via systemd timer every 2 hours.
set -euo pipefail

_sed_i() { if [[ "$OSTYPE" == "darwin"* ]]; then sed -i '' "$@"; else sed -i "$@"; fi; }

CREDS="$HOME/.claude/.credentials.json"
OPENCLAW_JSON="$HOME/.openclaw/openclaw.json"
OPENCLAW_ENV="$HOME/.openclaw/.env"
OVERRIDE_CONF="$HOME/.config/systemd/user/openclaw-gateway.service.d/override.conf"

if [ ! -f "$CREDS" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Claude credentials not found at $CREDS"
    exit 1
fi

# Read current token and expiry from Claude Code credentials
ACCESS_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS'))['claudeAiOauth']['accessToken'])")
EXPIRES_AT=$(python3 -c "import json; print(json.load(open('$CREDS'))['claudeAiOauth']['expiresAt'])")

REMAINING=$(python3 -c "import time; print(f'{($EXPIRES_AT/1000 - time.time())/3600:.1f}h')")
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Current token: ${ACCESS_TOKEN:0:20}... (expires in $REMAINING)"

# Check what's currently in the systemd override
OLD_TOKEN=""
if [ -f "$OVERRIDE_CONF" ]; then
    OLD_TOKEN=$(grep "ANTHROPIC_API_KEY=" "$OVERRIDE_CONF" 2>/dev/null | sed 's/.*ANTHROPIC_API_KEY=//' || true)
fi

if [ "$OLD_TOKEN" = "$ACCESS_TOKEN" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Token already synced, no changes needed"
    exit 0
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Token mismatch detected, syncing..."
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Old: ${OLD_TOKEN:0:20}..."
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] New: ${ACCESS_TOKEN:0:20}..."

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
echo "[sync] Updated .env"

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
echo "[sync] Updated systemd override"

# 4. Reload and restart gateway
systemctl --user daemon-reload
systemctl --user restart openclaw-gateway

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Gateway restarted with synced token (expires in $REMAINING)"
