#!/bin/bash
# proxy-monitor.sh — Quick health check for nvidia-proxy tuning
# Usage: ./proxy-monitor.sh [minutes]  (default: last 30 minutes)
MINS=${1:-30}
if [[ "$OSTYPE" == "darwin"* ]]; then
    SINCE=$(date -v-${MINS}M '+%Y-%m-%d %H:%M:%S')
else
    SINCE=$(date -d "$MINS minutes ago" '+%Y-%m-%d %H:%M:%S')
fi

echo "=== NVIDIA Proxy Monitor (last ${MINS}m) ==="
echo ""

# Request count & model breakdown
echo "--- Requests by Model ---"
journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -oP 'model=\K[^ ]+' | sort | uniq -c | sort -rn
echo ""

# Body size stats
echo "--- Body Sizes (bytes) ---"
SIZES=$(journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -oP 'bodyLen=\K[0-9]+' | sort -n)
if [ -n "$SIZES" ]; then
  COUNT=$(echo "$SIZES" | wc -l)
  MIN=$(echo "$SIZES" | head -1)
  MAX=$(echo "$SIZES" | tail -1)
  AVG=$(echo "$SIZES" | awk '{s+=$1} END {printf "%.0f", s/NR}')
  echo "  count=$COUNT  min=${MIN}  avg=${AVG}  max=${MAX}  limit=120000"
  if [ "$MAX" -gt 100000 ]; then
    echo "  ⚠️  Max approaching limit — consider bumping MAX_BODY_BYTES"
  elif [ "$MAX" -lt 40000 ]; then
    echo "  ✅ Plenty of headroom — no conversation trimming needed"
  else
    echo "  👀 Moderate usage — monitor for growth"
  fi
else
  echo "  (no requests)"
fi
echo ""

# Trimming events
echo "--- Trimming Events ---"
CONV_TRIM=$(journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -c "trimmed history")
AGGRESSIVE=$(journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -c "AGGRESSIVE")
SYS_TRIM=$(journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -c "trimmed system prompt")
TOOL_LIMIT=$(journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -c "TOOL LIMIT")
echo "  conversation trims: $CONV_TRIM"
echo "  aggressive trims:   $AGGRESSIVE"
echo "  system prompt trims: $SYS_TRIM"
echo "  tool limit hits:    $TOOL_LIMIT"
if [ "$AGGRESSIVE" -gt 0 ]; then
  echo "  ⚠️  Aggressive trims happening — bump MAX_BODY_BYTES or keepEnd"
elif [ "$CONV_TRIM" -gt 0 ]; then
  echo "  👀 Some conversation trimming — watch if it increases"
else
  echo "  ✅ No conversation trimming — settings have headroom"
fi
echo ""

# Error/retry stats
echo "--- Errors & Retries ---"
RETRIES=$(journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -c "attempt=[2-4]")
ERRORS=$(journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -cE "4[0-9]{2}|5[0-9]{2}|error|Error")
OK=$(journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -c "200 OK")
echo "  200 OK:  $OK"
echo "  retries: $RETRIES"
echo "  errors:  $ERRORS"
echo ""

# Response times (rough — from consecutive timestamps)
echo "--- Keyword Activations ---"
journalctl --user -u nvidia-proxy --no-pager --since "$SINCE" 2>/dev/null \
  | grep -oP 'keyword-activated tools: \[\K[^\]]+' \
  | tr ',' '\n' | sort | uniq -c | sort -rn | head -10
echo ""

# Current settings
echo "--- Current Proxy Settings ---"
grep -E "MAX_BODY_BYTES|MAX_SYSTEM_BYTES|allTools.length >|counter >= |keepEnd.*Math" \
  /home/cbrd21/clawd/skcapstone-repos/skcapstone/scripts/nvidia-proxy.mjs 2>/dev/null \
  | sed 's/^[[:space:]]*/  /'
