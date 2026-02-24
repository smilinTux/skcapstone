#!/usr/bin/env bash
# -------------------------------------------------------------------
# skcapstone MCP server launcher (tool-agnostic)
#
# Works with: Cursor, Claude Desktop, Claude Code CLI, Windsurf,
#             Aider, Cline, or any MCP client that speaks stdio.
#
# The script auto-detects the Python virtualenv and launches the
# MCP server on stdio. No hardcoded paths required in client configs.
#
# Usage:
#   ./skcapstone/scripts/mcp-serve.sh          (from repo root)
#   bash skcapstone/scripts/mcp-serve.sh       (explicit bash)
# -------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKCAPSTONE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- Locate the virtualenv ---
# Priority: SKCAPSTONE_VENV env var > first venv with mcp installed
# Candidates: skmemory/.venv (shared project venv) > skcapstone/.venv > repo .venv
find_venv() {
    if [[ -n "${SKCAPSTONE_VENV:-}" ]] && [[ -f "$SKCAPSTONE_VENV/bin/python" ]]; then
        echo "$SKCAPSTONE_VENV"
        return
    fi

    local candidates=(
        "$REPO_ROOT/skmemory/.venv"
        "$SKCAPSTONE_DIR/.venv"
        "$REPO_ROOT/.venv"
    )

    for venv in "${candidates[@]}"; do
        if [[ -f "$venv/bin/python" ]]; then
            if "$venv/bin/python" -c "import mcp" 2>/dev/null; then
                echo "$venv"
                return
            fi
        fi
    done

    # Fallback: return first venv that exists (may need pip install mcp)
    for venv in "${candidates[@]}"; do
        if [[ -f "$venv/bin/python" ]]; then
            echo "$venv"
            return
        fi
    done

    return 1
}

VENV_DIR="$(find_venv)" || {
    echo "ERROR: No Python virtualenv found." >&2
    echo "Create one with: python -m venv skcapstone/.venv && skcapstone/.venv/bin/pip install -e skcapstone/" >&2
    exit 1
}

PYTHON="$VENV_DIR/bin/python"

# --- Ensure skcapstone is importable ---
export PYTHONPATH="${SKCAPSTONE_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

# --- Launch MCP server on stdio ---
exec "$PYTHON" -m skcapstone.mcp_server "$@"
