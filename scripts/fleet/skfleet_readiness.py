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
import shlex
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


def parse_systemd_environment(raw: str) -> dict[str, str]:
    """Parse the output of `systemctl show <unit> -p Environment --value`.

    That output is a single line of space-separated KEY=value pairs (systemd
    already merges the unit file and every drop-in that touches it). Each
    pair is split on the FIRST '=' only, so a value that itself contains an
    '=' (for example a URL with a query string) stays intact.
    """
    raw = raw.strip()
    if not raw:
        return {}
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    env: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        env[key] = value
    return env


def systemd_effective_environment(unit: str) -> tuple[dict[str, str] | None, str | None]:
    """Ask systemd for the effective environment of a unit: the unit file plus
    every drop-in that touches it, already merged by systemd itself.

    Returns (env, None) on success, or (None, message) when the environment
    could not be determined (systemctl missing, unit unknown, or any other
    failure). Never raises. An undetermined state is never reported as an
    empty-but-successful environment; callers must treat (None, message) as
    a failed check, not as "no vars required."
    """
    # Two separate calls, one property each. systemctl show --value does NOT
    # return lines in the order properties were given on the command line
    # (confirmed by hand: -p LoadState -p Environment and -p Environment
    # -p LoadState both printed Environment first), so asking for both
    # properties in one call and reading lines by position is not reliable.
    load_state, err = _systemctl_show_value(unit, "LoadState")
    if err is not None:
        return None, err

    if load_state.strip() in ("", "not-found"):
        return None, "systemd does not know unit %s (LoadState=%s)" % (unit, load_state.strip() or "unknown")

    environment_line, err = _systemctl_show_value(unit, "Environment")
    if err is not None:
        return None, err

    return parse_systemd_environment(environment_line), None


def _systemctl_show_value(unit: str, prop: str) -> tuple[str, str | None]:
    """Run `systemctl --user show <unit> -p <prop> --value` and return
    (stdout, None) on success or ("", message) on failure. Never raises.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", prop, "--value"],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", "systemctl is not available (%s)" % exc

    if proc.returncode != 0:
        stderr = proc.stderr.strip() or ("systemctl exited with code %d" % proc.returncode)
        return "", "systemctl could not read unit %s (%s)" % (unit, stderr)

    return proc.stdout, None


def _iter_unit_files(units_dir: Path):
    for path in sorted(units_dir.glob("*.service")):
        yield path


def _run(
    rotate_script: Path,
    units_dir: Path,
    python_bin: str,
    env_from_unit: str | None,
    env_from_systemd: str | None = None,
):
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

    env_error: str | None = None
    if env_from_systemd:
        env_snapshot, env_error = systemd_effective_environment(env_from_systemd)
        env_source_label = "systemd effective environment for unit %s" % env_from_systemd
    elif env_from_unit:
        unit_path = units_dir / env_from_unit
        env_snapshot = _env_from_unit_file(unit_path)
        env_source_label = "unit %s (Environment= lines only, drop-ins not read)" % env_from_unit
    else:
        env_snapshot = dict(os.environ)
        env_source_label = "process environment"

    if env_error is not None:
        ok = False
        lines.append(
            "FAIL required env: could not determine %s (%s); "
            "an undetermined environment is never treated as ready" % (env_source_label, env_error)
        )
        for name in sorted(mandatory):
            lines.append(
                "FAIL required env: %s could not be verified (environment source unavailable)" % name
            )
    else:
        if not mandatory:
            lines.append("OK required env: dispatcher declares no mandatory env vars")
        for name in sorted(mandatory):
            if env_snapshot is not None and name in env_snapshot and env_snapshot[name] != "":
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
    parser.add_argument("--env-from-unit", default=None, help="Optional unit file name; check env against its Environment= lines only (drop-ins are not read), instead of the process environment")
    parser.add_argument("--env-from-systemd", default=None, help="Optional systemd unit name; check env against the unit's EFFECTIVE environment as reported by systemctl --user show, which already merges the unit file and every drop-in. Preferred over --env-from-unit when both are given.")
    args = parser.parse_args(argv)

    return _run(
        Path(args.rotate_script),
        Path(args.units_dir),
        args.python_bin,
        args.env_from_unit,
        args.env_from_systemd,
    )


if __name__ == "__main__":
    sys.exit(main())
