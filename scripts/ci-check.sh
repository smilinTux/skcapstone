#!/usr/bin/env bash
# ci-check.sh — Run the same checks as GitHub Actions CI locally.
# Usage: bash scripts/ci-check.sh
# Run this before committing/pushing to catch failures early.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

FAIL=0

echo -e "${YELLOW}=== SKCapstone CI Check ===${NC}"
echo ""

# 1. Test collection — make sure all tests can be imported
echo -e "${YELLOW}[1/4] Test collection...${NC}"
if python -m pytest tests/ --collect-only -q 2>&1 | tail -1 | grep -q "error"; then
    echo -e "${RED}FAIL: Test collection errors${NC}"
    python -m pytest tests/ --collect-only -q 2>&1 | grep -i error
    FAIL=1
else
    COUNT=$(python -m pytest tests/ --collect-only -q 2>&1 | tail -1 | grep -oP '\d+ test' | head -1)
    echo -e "${GREEN}OK: ${COUNT}s collected${NC}"
fi

# 2. Lint check
echo ""
echo -e "${YELLOW}[2/4] Ruff lint...${NC}"
if ruff check src/ 2>&1; then
    echo -e "${GREEN}OK: No lint errors${NC}"
else
    echo -e "${RED}FAIL: Lint errors found${NC}"
    FAIL=1
fi

# 3. Build check
echo ""
echo -e "${YELLOW}[3/4] Build check...${NC}"
if python -m build --no-isolation 2>&1 | tail -1 | grep -q "Successfully"; then
    echo -e "${GREEN}OK: Package builds${NC}"
    rm -rf dist/ build/ *.egg-info
else
    echo -e "${RED}FAIL: Build failed${NC}"
    FAIL=1
fi

# 4. Version consistency
echo ""
echo -e "${YELLOW}[4/4] Version consistency...${NC}"
PY_VER=$(python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])" 2>/dev/null || python3 -c "import tomli; print(tomli.load(open('pyproject.toml','rb'))['project']['version'])")
JS_VER=$(python -c "import json; print(json.load(open('package.json'))['version'])")
if [ "$PY_VER" = "$JS_VER" ]; then
    echo -e "${GREEN}OK: pyproject.toml ($PY_VER) == package.json ($JS_VER)${NC}"
else
    echo -e "${RED}FAIL: Version mismatch — pyproject.toml=$PY_VER package.json=$JS_VER${NC}"
    FAIL=1
fi

echo ""
if [ $FAIL -eq 0 ]; then
    echo -e "${GREEN}=== ALL CHECKS PASSED ===${NC}"
else
    echo -e "${RED}=== CHECKS FAILED ===${NC}"
    exit 1
fi
