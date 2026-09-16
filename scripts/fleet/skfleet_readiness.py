#!/usr/bin/env python3
"""Fleet readiness gate.

Before a rollout goes to more than one host, this checks that a node has
everything the dispatcher will need at runtime: every mandatory environment
variable the dispatcher requires, and every module every systemd unit's
ExecStart names, actually importable under the target Python.

Two things went wrong in production that this exists to catch:
  1. A node came up FAILING because main required SKFLEET_GATEWAY_URL and no
     systemd unit supplied it.
  2. skfleet-niobe-shadow.service names a module, skcapstone.seat_shadow_entrypoint,
     that does not exist on main. Nothing caught a unit pointing at a missing
     module.

Python 3.12, standard library only.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

EXEC_START_MODULE_RE = re.compile(r"-m\s+([A-Za-z_][\w.]*)")


def required_env(source: str) -> set[str]:
    """Parse dispatcher source text and return mandatory env var names.

    A name is mandatory when either:
      - it is the first positional argument to a call to
        ``_required_lane_target(...)`` and that call has no ``default=``
        keyword argument, or
      - it appears in ``raise SystemExit("NAME is required")``.
    """
    names: set[str] = set()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = func.id if isinstance(func, ast.Name) else None
            if func_name != "_required_lane_target":
                continue
            has_default = any(kw.arg == "default" for kw in node.keywords)
            if has_default:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)

    for match in re.finditer(r'raise\s+SystemExit\(\s*"([^"]+?)\s+is required"\s*\)', source):
        names.add(match.group(1))
    for match in re.finditer(r"raise\s+SystemExit\(\s*'([^']+?)\s+is required'\s*\)", source):
        names.add(match.group(1))

    return names


def unit_modules(unit_text: str) -> list[str]:
    """Return every ``python -m <module>`` / ``python3 -m <module>`` module path
    found in ExecStart= lines, in order, with no duplicates.
    """
    modules: list[str] = []
    seen: set[str] = set()
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("ExecStart="):
            continue
        for match in EXEC_START_MODULE_RE.finditer(stripped):
            module = match.group(1)
            if module not in seen:
                seen.add(module)
                modules.append(module)
    return modules


def check_module_imports(modules: list[str], python_bin: str) -> dict[str, bool]:
    """Return {module: True/False} for whether each imports under python_bin.

    Never raises: a subprocess failure of any kind is recorded as False.
    """
    results: dict[str, bool] = {}
    for module in modules:
        code = "import importlib; importlib.import_module('%s')" % module
        try:
            proc = subprocess.run(
                [python_bin, "-c", code],
                capture_output=True,
                text=True,
            )
            results[module] = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            results[module] = False
    return results


def _iter_unit_files(units_dir: Path):
    for path in sorted(units_dir.glob("*.service")):
        yield path


def _run(rotate_script: Path, units_dir: Path, python_bin: str, env_from_unit: str | None):
    lines: list[str] = []
    ok = True

    try:
        dispatcher_source = rotate_script.read_text(encoding="utf-8")
    except OSError as exc:
        lines.append("FAIL dispatcher source: could not read %s (%s)" % (rotate_script, exc))
        print("\n".join(lines))
        print("NOT READY")
        return 1

    mandatory = required_env(dispatcher_source)

    if env_from_unit:
        unit_path = units_dir / env_from_unit
        env_snapshot = _env_from_unit_file(unit_path)
        env_source_label = "unit %s" % env_from_unit
    else:
        env_snapshot = dict(os.environ)
        env_source_label = "process environment"

    if not mandatory:
        lines.append("OK required env: dispatcher declares no mandatory env vars")
    for name in sorted(mandatory):
        if name in env_snapshot and env_snapshot[name] != "":
            lines.append("OK required env: %s is set (checked against %s)" % (name, env_source_label))
        else:
            ok = False
            lines.append(
                "FAIL required env: %s is missing (checked against %s); "
                "dispatcher will raise SystemExit without it" % (name, env_source_label)
            )

    all_modules: list[str] = []
    seen_modules: set[str] = set()
    unit_files = list(_iter_unit_files(units_dir))
    if not unit_files:
        lines.append("FAIL units: no *.service files found under %s" % units_dir)
        ok = False
    for unit_path in unit_files:
        try:
            unit_text = unit_path.read_text(encoding="utf-8")
        except OSError as exc:
            ok = False
            lines.append("FAIL unit %s: could not read file (%s)" % (unit_path.name, exc))
            continue
        modules = unit_modules(unit_text)
        for module in modules:
            if module not in seen_modules:
                seen_modules.add(module)
                all_modules.append(module)

    import_results = check_module_imports(all_modules, python_bin)

    for unit_path in unit_files:
        try:
            unit_text = unit_path.read_text(encoding="utf-8")
        except OSError:
            continue
        modules = unit_modules(unit_text)
        if not modules:
            continue
        for module in modules:
            imports_ok = import_results.get(module, False)
            if imports_ok:
                lines.append("OK unit %s: module %s imports under %s" % (unit_path.name, module, python_bin))
            else:
                ok = False
                lines.append(
                    "FAIL unit %s: module %s does not import under %s; "
                    "ExecStart will fail at runtime" % (unit_path.name, module, python_bin)
                )

    print("\n".join(lines))
    print("READY" if ok else "NOT READY")
    return 0 if ok else 1


def _env_from_unit_file(unit_path: Path) -> dict[str, str]:
    """Best-effort extraction of Environment= assignments from a unit file,
    layered on top of the current process environment.
    """
    env = dict(os.environ)
    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError:
        return env
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        remainder = stripped[len("Environment="):].strip()
        if remainder.startswith('"') and remainder.endswith('"'):
            remainder = remainder[1:-1]
        if "=" in remainder:
            key, _, value = remainder.partition("=")
            env[key.strip()] = value.strip()
    return env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fleet readiness gate for a node before rollout continues.")
    parser.add_argument("--rotate-script", required=True, help="Path to the dispatcher source, e.g. scripts/fleet/skfleet-rotate.py")
    parser.add_argument("--units-dir", required=True, help="Directory containing systemd *.service unit files")
    parser.add_argument("--python-bin", required=True, help="Python interpreter to check module imports against")
    parser.add_argument("--env-from-unit", default=None, help="Optional unit file name; check env against its Environment= lines instead of the process environment")
    args = parser.parse_args(argv)

    return _run(
        Path(args.rotate_script),
        Path(args.units_dir),
        args.python_bin,
        args.env_from_unit,
    )


if __name__ == "__main__":
    sys.exit(main())
