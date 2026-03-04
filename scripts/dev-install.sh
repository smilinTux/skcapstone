#!/bin/bash
# dev-install.sh — Sovereign Agent Suite Dev Installer
#
# Wrapper around install.sh with --dev flag.
# Installs all SK* packages plus pytest, ruff, black.
#
# Usage:
#   bash scripts/dev-install.sh
#   bash scripts/dev-install.sh --force   # Recreate venv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "$SCRIPT_DIR/install.sh" --dev "$@"
