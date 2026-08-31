#!/usr/bin/bash
# Pip wrapper that blocks editable installs into shared interpreters.
#
# This wrapper is installed as ~/.local/bin/pip on service hosts.
# It checks if the install is in editable mode into a shared interpreter
# and blocks it with a clear error message.
#
# Installation (on service hosts):
#   ln -s ~/.skcapstone/scripts/pip-editable-guard.sh ~/.local/bin/pip
#
# To bypass (with justification):
#   SKIP_EDITABLE_GUARD=1 pip install -e . --no-deps

set -euo pipefail

# Paths that are shared interpreters (must match Python module)
SHARED_VENV="$HOME/.skenv"

# Service hostnames (must match Python module)
SERVICE_HOSTS="chiap01|chiap02|chiap03|chiap04|chiap08|chiwk11|chiwk13"

# Get current hostname
HOSTNAME="${HOSTNAME:-$(hostname)}"

# Check if this is a service host
if echo "$HOSTNAME" | grep -qE "^($SERVICE_HOSTS)$"; then
    IS_SERVICE_HOST=true
else
    IS_SERVICE_HOST=false
fi

# Get the actual Python interpreter being used
if [ -n "${VIRTUAL_ENV:-}" ]; then
    CURRENT_VENV="$VIRTUAL_ENV"
elif [[ "$(which python)" == "$SHARED_VENV"* ]]; then
    CURRENT_VENV="$SHARED_VENV"
else
    # Not using a known venv, allow everything
    exec /usr/bin/env pip "$@"
fi

# Check if we're in the shared venv on a service host
if [ "$IS_SERVICE_HOST" = true ] && [ "$CURRENT_VENV" = "$SHARED_VENV" ]; then
    # Check for editable install
    EDITABLE=0
    for arg in "$@"; do
        case "$arg" in
            -e|--editable)
                EDITABLE=1
                ;;
            --editable=*)
                EDITABLE=1
                ;;
        esac
    done

    if [ "$EDITABLE" = 1 ]; then
        # Check for bypass
        if [ "${SKIP_EDITABLE_GUARD:-0}" = "1" ]; then
            echo "WARNING: Skipping editable install guard (SKIP_EDITABLE_GUINT=1)" >&2
            echo "Please ensure this is justified and documented." >&2
        else
            cat >&2 <<'EOF'
BLOCKED: pip install -e is not allowed into the shared .skenv on service hosts.

This pattern has caused multiple outages:
- 2026-08-30: chiap04 skdashboard HTTP 500 from workspace change
- 2026-08-31: sklegal packages running from orphaned worktree

Alternatives:
1. Build a wheel and install non-editable: pip install .
2. Use a private venv for development: python -m venv .venv
3. Use pip install --user to install to ~/.local instead of shared venv

To bypass (with justification):
  SKIP_EDITABLE_GUARD=1 pip install -e . --no-deps

EOF
            exit 1
        fi
    fi
fi

# Pass through to real pip
exec /usr/bin/env pip "$@"
