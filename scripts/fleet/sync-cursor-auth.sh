#!/usr/bin/env bash
# Push the Cursor CLI credential from the owner login host into a credential
# slot on another host. One script for every slot; the unit names the slot:
#
#   CURSOR_AUTH_DEST_HOST  ssh host to push to (required)
#   CURSOR_AUTH_DEST       path under that host's $HOME
#                          (default skgateway-codex/secrets/cursor-auth.json,
#                          the gateway slot; a worker uses .config/cursor/auth.json)
#   CURSOR_AUTH_SRC        source file (default ~/.config/cursor/auth.json)
#   CURSOR_AUTH_VALIDATE_ONLY=1  validate the source and stop
#
# Validate before install, atomic replacement, 600 permissions, hash-keyed
# rollback copies, never print or persist token values.
#
# Exit codes: 0 pushed; 1 transport or install failure; 2 the source credential
# is unusable (missing, not a JWT, expired, no refresh token). Exit 2 needs a
# person: the Cursor login on the source host has to be renewed with
# `cursor-agent login` there (`cursor-agent status` can still say "Logged in"
# then; `cursor-agent models` tells the truth). Until 2026-11-15 an expired
# token surfaced as a Python AssertionError traceback every 10 minutes, which
# read like a script bug rather than "log in again".
set -euo pipefail

DEST_HOST="${CURSOR_AUTH_DEST_HOST:-}"
SRC_PATH="${CURSOR_AUTH_SRC:-$HOME/.config/cursor/auth.json}"
DEST_PATH="${CURSOR_AUTH_DEST:-skgateway-codex/secrets/cursor-auth.json}"
ROLLBACK_PATH="${CURSOR_AUTH_ROLLBACK_DIR:-$(dirname "$DEST_PATH")/rollback}"

TMP="$(mktemp)"
REMOTE_SCRIPT="$(mktemp)"
STAGE="/tmp/.cursor-auth-sync.$$.json"
trap 'rm -f "$TMP" "$REMOTE_SCRIPT"' EXIT

if [[ -z "$DEST_HOST" && "${CURSOR_AUTH_VALIDATE_ONLY:-}" != 1 ]]; then
  echo "FAIL: CURSOR_AUTH_DEST_HOST is not set; the unit must name the slot" >&2
  exit 1
fi

relogin="renew the Cursor login on $(hostname -s) with 'cursor-agent login'"

if [[ ! -f "$SRC_PATH" ]]; then
  echo "FAIL: $SRC_PATH missing; $relogin" >&2
  exit 2
fi

# Validation prints one line and exits 2; it never raises a traceback.
python3 - "$SRC_PATH" "$TMP" "$relogin" <<'PY' || exit $?
import base64, json, os, sys, time

src, out, relogin = sys.argv[1], sys.argv[2], sys.argv[3]


def fail(reason: str) -> None:
    print(f"FAIL: {src}: {reason}; {relogin}", file=sys.stderr)
    sys.exit(2)


try:
    with open(src) as handle:
        d = json.load(handle)
except (OSError, ValueError) as exc:
    fail(f"unreadable ({type(exc).__name__})")
tok = d.get("accessToken") if isinstance(d, dict) else None
if not (isinstance(tok, str) and tok.count(".") == 2):
    fail("no JWT accessToken")
try:
    part = tok.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    exp = int(claims.get("exp") or 0)
except (ValueError, TypeError):
    fail("accessToken is not a readable JWT")
if exp <= int(time.time()):
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)) if exp else "unknown"
    fail(f"accessToken expired at {when}")
if not d.get("refreshToken"):
    fail("no refreshToken")
fd = os.open(out, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o600)
with os.fdopen(fd, "w") as handle:
    json.dump(d, handle, indent=2)
PY

if [[ "${CURSOR_AUTH_VALIDATE_ONLY:-}" == 1 ]]; then
  echo "OK: $SRC_PATH is valid (validate only)"
  exit 0
fi

scp -o ConnectTimeout=8 -o BatchMode=yes "$TMP" "$DEST_HOST:$STAGE" >/dev/null 2>&1 || {
  echo "FAIL: cannot reach $DEST_HOST" >&2
  exit 1
}

cat > "$REMOTE_SCRIPT" <<REMOTE
set -e
STAGE='$STAGE'
DEST="\$HOME/$DEST_PATH"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['accessToken'] and d['refreshToken']" "\$STAGE"
mkdir -p "\$(dirname "\$DEST")"
chmod 700 "\$(dirname "\$DEST")"
if [[ -f "\$DEST" ]] && ! cmp -s "\$STAGE" "\$DEST"; then
  ROLLBACK="\$HOME/$ROLLBACK_PATH"
  mkdir -p "\$ROLLBACK"
  chmod 700 "\$ROLLBACK"
  HASH="\$(sha256sum "\$DEST" | cut -d' ' -f1)"
  install -m 600 "\$DEST" "\$ROLLBACK/\$HASH.json"
fi
install -m 600 "\$STAGE" "\$DEST.new"
mv -f "\$DEST.new" "\$DEST"
rm -f "\$STAGE"
test "\$(stat -c %a "\$DEST")" = 600
REMOTE

ssh -o ConnectTimeout=8 -o BatchMode=yes "$DEST_HOST" 'bash -s' < "$REMOTE_SCRIPT" || {
  echo "FAIL: destination validation or install failed on $DEST_HOST" >&2
  exit 1
}
echo "OK: cursor credential pushed to $DEST_HOST:$DEST_PATH"
