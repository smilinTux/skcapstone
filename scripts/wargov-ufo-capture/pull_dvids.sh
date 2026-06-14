#!/usr/bin/env bash
# Pull DVIDS media for Release 02 in parallel (up to 4 at a time).
# Reads inventory from probe-out/release-02-records.json.

set -u
OUT_DIR="$HOME/nextcloud/cbrd21-share/reference/war-gov-UFO-PURSUE-2026/release-02"
INVENTORY="$HOME/clawd/tmp/wargov-capture/probe-out/release-02-records.json"
LOG="$HOME/clawd/tmp/wargov-capture/probe-out/dvids-pull.log"
mkdir -p "$OUT_DIR"
: > "$LOG"

pull_one() {
  local dvids_id="$1"
  local title_slug="$2"
  local kind="$3"  # video or audio
  local out_path="$OUT_DIR/dvids-${kind}-${dvids_id}-${title_slug}"

  # Pick the right URL prefix
  local page_url
  if [[ "$kind" == "audio" ]]; then
    page_url="https://www.dvidshub.net/audio/${dvids_id}"
  else
    page_url="https://www.dvidshub.net/video/${dvids_id}"
  fi

  local page_html
  page_html=$(curl -sSL --max-time 60 "$page_url" 2>/dev/null) || {
    echo "[FAIL fetch page] dvids=$dvids_id" | tee -a "$LOG"
    return 1
  }

  # Extract mp4 (video) or mp3 (audio) CDN URL
  local media_url ext
  if [[ "$kind" == "audio" ]]; then
    media_url=$(echo "$page_html" | grep -oE 'https?://[^"]+\.mp3[^"]*' | head -1)
    ext="mp3"
    # DVIDS audio sometimes is .m4a or hosted via a different path; fallback to grepping for asset URL
    if [[ -z "$media_url" ]]; then
      media_url=$(echo "$page_html" | grep -oE 'https?://[^"]+\.m4a[^"]*' | head -1)
      ext="m4a"
    fi
    if [[ -z "$media_url" ]]; then
      media_url=$(echo "$page_html" | grep -oE 'https?://d34w7g4gy10iej\.cloudfront\.net/[^"]+' | head -1)
      ext="${media_url##*.}"
    fi
  else
    media_url=$(echo "$page_html" | grep -oE 'https?://[^"]+\.mp4[^"]*' | head -1)
    ext="mp4"
  fi

  if [[ -z "$media_url" ]]; then
    echo "[FAIL no-media-url] dvids=$dvids_id kind=$kind" | tee -a "$LOG"
    # Save the page HTML for inspection
    echo "$page_html" > "${out_path}.html"
    return 1
  fi

  local final_path="${out_path}.${ext}"
  if [[ -f "$final_path" ]] && [[ -s "$final_path" ]]; then
    echo "[SKIP already-have] $final_path" | tee -a "$LOG"
    return 0
  fi

  curl -sSL --max-time 600 -o "$final_path" "$media_url" 2>/dev/null
  local size
  size=$(stat -c '%s' "$final_path" 2>/dev/null || echo 0)
  if [[ "$size" -lt 1024 ]]; then
    echo "[FAIL download too-small=$size] dvids=$dvids_id url=$media_url" | tee -a "$LOG"
    return 1
  fi
  echo "[OK] dvids=$dvids_id kind=$kind size=$((size/1024))KB → $(basename "$final_path")" | tee -a "$LOG"
}

# Generate worker commands from the inventory using python
python3 - <<'PY' > /tmp/wargov-dvids-jobs.txt
import json, re
with open("/home/cbrd21/clawd/tmp/wargov-capture/probe-out/release-02-records.json") as f:
    records = json.load(f)
for r in records:
    if not r["dvids_id"]:
        continue
    kind = "audio" if r["type"] == "AUD" else "video"
    # Slugify title: strip quotes, collapse non-alnum to dashes, limit length
    title = r["title"]
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', title).strip('-').lower()[:60]
    # Use the DOW-UAP-PR id from the title if available (more durable)
    m = re.match(r'([A-Z]+-UAP-(?:PR|D)[0-9]+[a-z]?)', title)
    if m:
        slug = m.group(1).lower() + "-" + slug[:30]
    print(f"{r['dvids_id']}\t{slug}\t{kind}")
PY

JOB_COUNT=$(wc -l < /tmp/wargov-dvids-jobs.txt)
echo "[plan] $JOB_COUNT DVIDS jobs queued" | tee -a "$LOG"

export -f pull_one
export OUT_DIR LOG

# Run with xargs -P 4 (parallelism 4)
cat /tmp/wargov-dvids-jobs.txt | while IFS=$'\t' read -r id slug kind; do
  echo "$id $slug $kind"
done | xargs -L 1 -P 4 -I {} bash -c 'set -- {}; pull_one "$1" "$2" "$3"'

OK_COUNT=$(grep -c '^\[OK\]' "$LOG" || true)
FAIL_COUNT=$(grep -cE '^\[FAIL' "$LOG" || true)
SKIP_COUNT=$(grep -c '^\[SKIP' "$LOG" || true)
echo "[done] OK=$OK_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT" | tee -a "$LOG"
