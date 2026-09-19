#!/usr/bin/env bash
# Sync the Kimi Code credential from the host where a human actually runs the
# Kimi login into the credential this host's skgateway instance reads.
#
# WHY THIS EXISTS, and why its absence was expensive: the unit that runs this
# script, skgateway-kimi-auth-sync.service, had been failing 203/EXEC because
# the script did not exist on this host at all. Its timer kept firing, so the
# failure repeated indefinitely and nothing alerted. Meanwhile the gateway had
# no kimi credential, every kimi route answered
#   503 "every owner backend is currently unavailable"
# and lane_health resolved kimi to unknown. Being fail-closed on unknown, the
# dispatcher deferred every kimi card forever: measured on chi 2026-09-18,
# chiap02 and chiap03 were launching ZERO workers while holding assigned cards,
# both deferring on no-compatible-healthy-lane:kimi.
#
# This is the same failure the codex sibling documents for 2026-08-27, where a
# missing script left the gateway serving a stale credential from a different
# account. That one was found and fixed; this twin was never written.
#
# So this script fails LOUDLY and refuses to install anything it cannot verify.
set -euo pipefail

SRC_HOST="${KIMI_AUTH_SRC_HOST:-chiap08}"
SRC_PATH="${KIMI_AUTH_SRC_PATH:-.kimi-code/credentials/kimi-code.json}"
DEST="${KIMI_AUTH_DEST:-$HOME/.kimi-code/credentials/kimi-code.json}"
UNIT="${KIMI_GATEWAY_UNIT:-skgateway-codex}"

TMP="$(umask 077; mktemp)"
trap 'rm -f "$TMP"' EXIT

# The gateway reads the CLI-owned file directly (credentials_path in
# skgateway-codex.yaml), so DEST is that same path rather than a separate
# secrets copy. SRC_HOST is the owner login host named in the unit description.
if [ "$SRC_HOST" = "local" ]; then
    if ! cat "$HOME/${SRC_PATH}" > "$TMP" 2>/dev/null; then
        echo "FATAL: could not read local ~/${SRC_PATH}" >&2
        exit 1
    fi
    echo "  source: local ~/${SRC_PATH}"
elif ! ssh -o BatchMode=yes -o ConnectTimeout=10 "skuser01@${SRC_HOST}" \
        "cat ~/${SRC_PATH}" > "$TMP" 2>/dev/null; then
    echo "FATAL: could not read ~/${SRC_PATH} from ${SRC_HOST}" >&2
    exit 1
fi

# Refuse to install anything that is not a usable credential. A truncated or
# empty file installed over a working one takes the kimi lane down estate-wide.
# Also enforce NEWEST-WINS, which the unit description asks for: if this host's
# own CLI refreshed more recently than the source, keep what is here. Copying a
# stale snapshot over a self-refreshing credential is how the codex incident
# happened.
python3 - "$TMP" "$DEST" <<'PY'
import datetime, json, os, sys

src_path, dest_path = sys.argv[1], sys.argv[2]

try:
    src = json.load(open(src_path))
except Exception as exc:
    sys.exit("FATAL: source is not valid JSON: %s" % exc)

if not src.get("refresh_token"):
    sys.exit("FATAL: source has no refresh_token; it would expire with no way to renew")
if not src.get("access_token"):
    sys.exit("FATAL: source has no access_token")


def expiry(doc):
    """expires_at as an aware UTC datetime, tolerating seconds or milliseconds."""
    raw = doc.get("expires_at")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1e11:          # milliseconds
        value /= 1000.0
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)


src_exp = expiry(src)
now = datetime.datetime.now(datetime.timezone.utc)
if src_exp is None:
    print("  source has no parseable expires_at; installing anyway")
elif src_exp <= now:
    sys.exit("FATAL: source access_token already expired at %s"
             % src_exp.strftime("%Y-%m-%d %H:%M UTC"))
else:
    print("  source access_token valid until %s (%.1f minutes)"
          % (src_exp.strftime("%Y-%m-%d %H:%M UTC"), (src_exp - now).total_seconds() / 60))

if os.path.exists(dest_path):
    try:
        dest_exp = expiry(json.load(open(dest_path)))
    except Exception:
        dest_exp = None
    if dest_exp and src_exp and dest_exp > src_exp:
        sys.exit("SKIP: local credential is NEWER (%s) than the source (%s); keeping it"
                 % (dest_exp.strftime("%H:%M UTC"), src_exp.strftime("%H:%M UTC")))
PY
rc=$?
if [ "$rc" -ne 0 ]; then
    # A SKIP is a success for newest-wins; only a FATAL is a failure.
    exit "$rc"
fi

if [ -f "$DEST" ] && cmp -s "$TMP" "$DEST"; then
    echo "  credential unchanged; not restarting ${UNIT}"
    exit 0
fi

mkdir -p "$(dirname "$DEST")"
chmod 700 "$(dirname "$DEST")"
[ -f "$DEST" ] && cp -p "$DEST" "${DEST}.bak-$(date +%Y%m%d-%H%M%S)"
install -m 600 "$TMP" "$DEST"
echo "  credential updated at ${DEST}"

# The gateway reads the credential file per request, but a restart is what
# clears a backend it has already marked unavailable. require_observed_health
# keeps kimi fail-closed until a probe succeeds, so without this the lane stays
# down even once the credential is correct.
systemctl --user restart "$UNIT"
sleep 3
if systemctl --user is-active --quiet "$UNIT"; then
    echo "  ${UNIT} restarted and active"
else
    echo "FATAL: ${UNIT} did not come back after the credential update" >&2
    exit 1
fi
