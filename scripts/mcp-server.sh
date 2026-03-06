#!/usr/bin/env bash
# -------------------------------------------------------------------
# skcapstone MCP server launcher — cross-platform (Linux / macOS)
# Task: e5f81637
#
# Works with: Cursor, Claude Desktop, Claude Code CLI, Windsurf,
#             Aider, Cline, or any MCP client that speaks stdio.
#
# The script auto-detects the Python virtualenv and launches the
# MCP server on stdio. No hardcoded paths required in client configs.
#
# Usage:
#   ./scripts/mcp-server.sh              (from repo root)
#   bash scripts/mcp-server.sh           (explicit bash)
#
# Environment overrides:
#   SKCAPSTONE_VENV=/path/to/venv  — force a specific virtualenv
#   SKMEMORY_HOME=/path/to/dir     — override memory storage location
#   SKCAPSTONE_LOG_LEVEL=DEBUG     — set log verbosity
# -------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKCAPSTONE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Locate the Python interpreter ---
# Priority:
#   1. SKCAPSTONE_VENV env var (explicit override)
#   2. ~/.skenv  (standard SK* suite venv)
#   3. Project-local .venv dirs
#   4. System python3 / python
find_python() {
    # 1. Explicit venv override
    if [[ -n "${SKCAPSTONE_VENV:-}" ]]; then
        local py="$SKCAPSTONE_VENV/bin/python"
        if [[ -x "$py" ]]; then
            echo "$py"
            return
        fi
        echo "WARNING: SKCAPSTONE_VENV=$SKCAPSTONE_VENV set but $py not found, falling back." >&2
    fi

    # 2. Standard ~/.skenv venv
    local skenv="$HOME/.skenv/bin/python"
    if [[ -x "$skenv" ]]; then
        if "$skenv" -c "import skcapstone" 2>/dev/null; then
            echo "$skenv"
            return
        fi
    fi

    # 3. Project-local venvs
    local candidates=(
        "$SKCAPSTONE_DIR/.venv/bin/python"
        "$SKCAPSTONE_DIR/../.venv/bin/python"
    )
    for py in "${candidates[@]}"; do
        if [[ -x "$py" ]]; then
            if "$py" -c "import skcapstone" 2>/dev/null; then
                echo "$py"
                return
            fi
        fi
    done

    # 4. System Python (must have skcapstone installed)
    for cmd in python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            if "$cmd" -c "import skcapstone" 2>/dev/null; then
                echo "$(command -v "$cmd")"
                return
            fi
        fi
    done

    return 1
}

PYTHON="$(find_python)" || {
    echo "ERROR: Could not find a Python interpreter with skcapstone installed." >&2
    echo "" >&2
    echo "Install with:" >&2
    echo "  bash scripts/install.sh" >&2
    echo "" >&2
    echo "Or point to an existing venv:" >&2
    echo "  SKCAPSTONE_VENV=/path/to/venv bash scripts/mcp-server.sh" >&2
    exit 1
}

# --- Set environment variables ---
export SKMEMORY_HOME="${SKMEMORY_HOME:-$HOME/.skcapstone/memory}"
export SKCAPSTONE_HOME="${SKCAPSTONE_HOME:-$HOME/.skcapstone}"

# Ensure skcapstone is importable even if not pip-installed
export PYTHONPATH="${SKCAPSTONE_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

# --- Launch MCP server on stdio ---
exec "$PYTHON" -m skcapstone.mcp_server "$@"
