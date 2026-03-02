#!/bin/bash
# verify_install.sh — Verify skcapstone pip install works cleanly.
# Creates a fresh venv, installs each package, runs --version, reports pass/fail.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="/tmp/skcapstone_verify_venv_$$"
PASS=0
FAIL=0
RESULTS=()

# ── helpers ────────────────────────────────────────────────────────────────
ok()   { echo "  [PASS] $*"; PASS=$((PASS + 1)); RESULTS+=("PASS: $*"); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); RESULTS+=("FAIL: $*"); }
skip() { echo "  [SKIP] $*"; RESULTS+=("SKIP: $*"); }

hr() { printf '%0.s─' {1..60}; echo; }

cleanup() { rm -rf "$VENV_DIR"; }
trap cleanup EXIT

# ── setup ──────────────────────────────────────────────────────────────────
hr
echo "  SKCapstone Install Verification"
echo "  Repo : $REPO_ROOT"
echo "  Venv : $VENV_DIR"
hr

# Require Python ≥ 3.10 — prefer pyenv global python3, then fallback names
for _py in python3 python python3.13 python3.12 python3.11 python3.10; do
  if _found=$(command -v "$_py" 2>/dev/null) && "$_found" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
    PYTHON="$_found"
    break
  fi
done
if [[ -z "${PYTHON:-}" ]]; then
  echo "ERROR: python3 >= 3.10 not found"; exit 1
fi
PY_VER=$("$PYTHON" -c "import sys; print('%d.%d' % sys.version_info[:2])")
echo "  Python: $PYTHON ($PY_VER)"
hr

# Create isolated venv
"$PYTHON" -m venv "$VENV_DIR" || { echo "ERROR: venv creation failed"; exit 1; }
PIP="$VENV_DIR/bin/pip"
PYEXE="$VENV_DIR/bin/python"

"$PIP" install --quiet --upgrade pip wheel setuptools 2>&1 | tail -1

# Common pip flags — prefer pre-built wheels, 60s per-package timeout
PIP_OPTS=(--quiet --prefer-binary --timeout=60)

# ── 1. Core skcapstone (base deps only) ───────────────────────────────────
echo
echo "Step 1: core install (click, mcp, pydantic, pyyaml, rich)"
if "$PIP" install "${PIP_OPTS[@]}" "$REPO_ROOT" 2>&1; then
  ok "pip install skcapstone (core)"
else
  fail "pip install skcapstone (core)"
fi

# Verify importable
if "$PYEXE" -c "import skcapstone; print('  version:', skcapstone.__version__)" 2>&1; then
  ok "import skcapstone"
else
  fail "import skcapstone"
fi

# Verify CLI entry point
if "$VENV_DIR/bin/skcapstone" --help > /dev/null 2>&1; then
  ok "skcapstone --help"
else
  fail "skcapstone --help"
fi

if "$VENV_DIR/bin/skcapstone" --version 2>&1 | grep -qE "[0-9]+\.[0-9]+"; then
  ok "skcapstone --version"
else
  fail "skcapstone --version"
fi

# Verify crush shim entry point
if "$VENV_DIR/bin/crush" --help > /dev/null 2>&1; then
  ok "crush --help"
else
  fail "crush --help (stub)"
fi

# ── 2. Optional extra: consciousness (watchdog) ────────────────────────────
echo
echo "Step 2: optional 'consciousness' extra"
if "$PIP" install "${PIP_OPTS[@]}" "$REPO_ROOT[consciousness]" 2>&1; then
  ok "pip install skcapstone[consciousness]"
  if "$PYEXE" -c "import watchdog; print('  watchdog:', watchdog.__version__)" 2>&1; then
    ok "import watchdog"
  else
    fail "import watchdog"
  fi
else
  fail "pip install skcapstone[consciousness]"
fi

# ── 3. Optional extra: shell (prompt_toolkit) ──────────────────────────────
echo
echo "Step 3: optional 'shell' extra"
if "$PIP" install "${PIP_OPTS[@]}" "$REPO_ROOT[shell]" 2>&1; then
  ok "pip install skcapstone[shell]"
  if "$PYEXE" -c "import prompt_toolkit; print('  prompt_toolkit:', prompt_toolkit.__version__)" 2>&1; then
    ok "import prompt_toolkit"
  else
    fail "import prompt_toolkit"
  fi
else
  fail "pip install skcapstone[shell]"
fi

# ── 4. Dev extras (pytest, ruff, black) ────────────────────────────────────
echo
echo "Step 4: dev extras"
if "$PIP" install "${PIP_OPTS[@]}" "$REPO_ROOT[dev]" 2>&1; then
  ok "pip install skcapstone[dev]"
  for pkg in pytest ruff black; do
    if "$PYEXE" -c "import $pkg" 2>&1; then
      ok "import $pkg"
    else
      fail "import $pkg"
    fi
  done
else
  fail "pip install skcapstone[dev]"
fi

# ── 5. Local sibling packages (skseed, skcomm) ─────────────────────────────
echo
echo "Step 5: local sibling packages (if present)"
for pkg_dir in "$REPO_ROOT/../skseed" "$REPO_ROOT/../skcomm" "$REPO_ROOT/../skmemory" "$REPO_ROOT/../skskills"; do
  pkg_name=$(basename "$pkg_dir")
  if [[ -f "$pkg_dir/pyproject.toml" ]] || [[ -f "$pkg_dir/setup.py" ]]; then
    if "$PIP" install "${PIP_OPTS[@]}" -e "$pkg_dir" 2>&1; then
      ok "pip install -e $pkg_name"
      if "$PYEXE" -c "import $pkg_name" 2>&1; then
        ok "import $pkg_name"
      else
        fail "import $pkg_name"
      fi
    else
      fail "pip install -e $pkg_name"
    fi
  else
    skip "$pkg_name not found at $pkg_dir"
  fi
done

# ── 6. Verify key CLI subcommands load ─────────────────────────────────────
echo
echo "Step 6: CLI subcommands smoke test"
SKCAP="$VENV_DIR/bin/skcapstone"
for subcmd in status memory coord doctor daemon sync; do
  if "$SKCAP" "$subcmd" --help > /dev/null 2>&1; then
    ok "skcapstone $subcmd --help"
  else
    fail "skcapstone $subcmd --help"
  fi
done

# ── Summary ────────────────────────────────────────────────────────────────
echo
hr
echo "  Results: $PASS passed, $FAIL failed"
hr
if [[ $FAIL -eq 0 ]]; then
  echo "  ALL CHECKS PASSED"
  exit 0
else
  echo "  FAILURES:"
  for r in "${RESULTS[@]}"; do
    [[ "$r" == FAIL:* ]] && echo "    $r"
  done
  exit 1
fi
