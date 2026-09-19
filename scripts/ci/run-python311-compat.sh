#!/usr/bin/env bash
set -euo pipefail

base="${1:?usage: run-python311-compat.sh BASE HEAD}"
head="${2:?usage: run-python311-compat.sh BASE HEAD}"
python_bin="${PYTHON_BIN:-python}"

"$python_bin" -m compileall -q src

# Build the install wheel off the live tree. In-tree `pip wheel .` writes
# src/skcapstone.egg-info; pytest sets pythonpath=["src"], so a setuptools-scm
# Version: 0.0.0 fallback there shadows the installed dist-info for later tests.
wheel_dir="$(mktemp -d)"
stage="$(mktemp -d)"
cleanup_wheel_stage() {
  rm -rf "$wheel_dir" "$stage"
}
trap cleanup_wheel_stage EXIT

# Prefer the already-installed package version over a fresh scm probe so this
# step does not write version files back into the live checkout.
pretend="$("$python_bin" -c 'from importlib.metadata import version; print(version("skcapstone"))')"
git archive HEAD | tar -x -C "$stage"
SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE="$pretend" \
  "$python_bin" -m pip wheel --no-deps --no-cache-dir --wheel-dir "$wheel_dir" "$stage"
"$python_bin" -m pip install --force-reinstall --no-deps "$wheel_dir"/*.whl
cleanup_wheel_stage
trap - EXIT

"$python_bin" - <<'PY'
import skcapstone
from skcapstone.models import AgentConfig, SyncConfig

assert skcapstone
assert AgentConfig
assert SyncConfig
PY

tests=(
  tests/test_card.py
  tests/test_config_validate.py
  tests/test_cross_package.py
  tests/test_models.py
  tests/test_version_check.py
  tests/test_version_cmd.py
  tests/test_version_single_source.py
)
while IFS= read -r path; do
  [[ -f "$path" ]] || continue
  case "$path" in
    tests/test_*.py|tests/*/test_*.py) tests+=("$path") ;;
  esac
done < <(git diff --name-only --diff-filter=ACMRT "$base" "$head" -- tests)

mapfile -t tests < <(printf '%s\n' "${tests[@]}" | sort -u)
printf 'Python 3.11 compatibility tests (%d files):\n' "${#tests[@]}"
printf '  %s\n' "${tests[@]}"
"$python_bin" -m pytest "${tests[@]}" --strict-markers -m "not integration and not e2e"
