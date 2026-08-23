#!/usr/bin/env bash
# check-no-shim-imports.sh (CR-3.6)
#
# Fails the build if any *.py under src/ or tests/ imports the retired capauth
# re-export shims. Those modules (tokens, trust_graph, trust_calibration) were
# deleted from skcapstone; their real implementations live in capauth:
#   skcapstone.tokens             -> capauth.tokens
#   skcapstone.trust_graph        -> capauth.trust.graph
#   skcapstone.trust_calibration  -> capauth.trust.calibration
#
# Any new import of the shim names is a regression. Import from capauth directly.
#
# Usage: bash scripts/check-no-shim-imports.sh

set -euo pipefail

# Run relative to the repo root (this script lives in scripts/).
cd "$(dirname "$0")/.."

# Forbidden import forms, matched only on actual import statements (leading
# whitespace allowed) so prose/docstrings mentioning the names never trip it.
# Relative forms with any depth of dots (. .. ...) are covered:
#   from skcapstone.tokens import ...            (and trust_graph/trust_calibration)
#   from .tokens import ... / from ..tokens import ...   (any dot depth)
#   import skcapstone.tokens[ as ...]            (and trust_graph/trust_calibration)
#   from skcapstone import tokens|trust_graph|trust_calibration
PATTERN='^[[:space:]]*(from[[:space:]]+(skcapstone\.|\.+)(tokens|trust_graph|trust_calibration)[[:space:]]+import|import[[:space:]]+skcapstone\.(tokens|trust_graph|trust_calibration)([[:space:]]|$)|from[[:space:]]+skcapstone[[:space:]]+import[[:space:]]+.*\b(tokens|trust_graph|trust_calibration)\b)'

HITS=$(grep -rnE "$PATTERN" --include='*.py' src tests || true)

if [ -n "$HITS" ]; then
    echo "FAIL: retired capauth shim imports found (CR-3.6)."
    echo "Import from capauth directly instead:"
    echo "  skcapstone.tokens            -> capauth.tokens"
    echo "  skcapstone.trust_graph       -> capauth.trust.graph"
    echo "  skcapstone.trust_calibration -> capauth.trust.calibration"
    echo ""
    echo "$HITS"
    exit 1
fi

echo "OK: no retired shim imports (tokens / trust_graph / trust_calibration)."
