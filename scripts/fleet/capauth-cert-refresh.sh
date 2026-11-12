#!/usr/bin/env bash
# Keep the CapAuth copy of the TLS cert in step with the Tailscale-managed one.
#
# Tailscale already owns and auto-renews the cert in /var/lib/tailscale/certs.
# CapAuth reads its own copy in ~/.config/capauth/tls, which does not follow a
# renewal. This re-copies ONLY when the serial actually changes, then restarts
# the consumer, and rolls back if that restart fails.
#
# Replaces the recurring manual renewal card (870ae7f3). No cert is ever
# issued here: tailscale returns its cached cert unless it is due for renewal.
set -euo pipefail

DEST="$HOME/.config/capauth/tls"
CONSUMER="capauth-oidc-qualification.service"

if [ ! -d "$DEST" ]; then
  echo "no CapAuth tls dir at $DEST, nothing to refresh" >&2
  exit 0
fi

DOMAIN="$(tailscale status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

sudo -n tailscale cert --cert-file "$TMP/fullchain.pem" --key-file "$TMP/privkey.pem" "$DOMAIN" >/dev/null
sudo -n chown "$(id -u):$(id -g)" "$TMP/fullchain.pem" "$TMP/privkey.pem"

new="$(openssl x509 -in "$TMP/fullchain.pem" -noout -serial)"
cur="$(openssl x509 -in "$DEST/fullchain.pem" -noout -serial 2>/dev/null || echo none)"

if [ "$new" = "$cur" ]; then
  echo "unchanged: $cur"
  exit 0
fi

echo "rotating: $cur -> $new"
cp -a "$DEST/fullchain.pem" "$TMP/prev-fullchain.pem" 2>/dev/null || true
cp -a "$DEST/privkey.pem"   "$TMP/prev-privkey.pem"   2>/dev/null || true

install -m 644 "$TMP/fullchain.pem" "$DEST/fullchain.pem"
install -m 600 "$TMP/privkey.pem"   "$DEST/privkey.pem"

systemctl --user restart "$CONSUMER"
sleep 3

# is-active --quiet returns non-zero while a unit is still activating, so match
# the reported state explicitly rather than trusting the exit code.
S="$(systemctl --user is-active "$CONSUMER" || true)"
case "$S" in
  active|activating)
    echo "ok: $new ($CONSUMER is $S)"
    ;;
  *)
    echo "consumer is $S after restart, rolling back to $cur" >&2
    [ -f "$TMP/prev-fullchain.pem" ] && install -m 644 "$TMP/prev-fullchain.pem" "$DEST/fullchain.pem"
    [ -f "$TMP/prev-privkey.pem" ]   && install -m 600 "$TMP/prev-privkey.pem"   "$DEST/privkey.pem"
    systemctl --user restart "$CONSUMER" || true
    exit 1
    ;;
esac
