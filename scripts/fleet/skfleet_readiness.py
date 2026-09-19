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
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
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


def unit_python(unit_text: str) -> str | None:
    """The interpreter a unit's own ExecStart names, if it names an absolute one.

    A unit is entitled to its own virtualenv, and declaring it in ExecStart is
    how it does so. Checking every unit's imports against ONE interpreter
    therefore produces false failures for any unit that is not in that venv.

    Measured on chiap04 2026-09-18: hermes-gateway.service runs
    ``/home/skuser01/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main``
    and had been active since 2026-08-27, yet readiness reported
    "module hermes_cli.main does not import" because it tested
    ``~/.skenv/bin/python3``. Under the unit's own interpreter the module
    imports fine. That false failure blocked the deploy gate for the whole
    host.

    Returns None when ExecStart does not name an absolute interpreter, so the
    caller falls back to the interpreter it was given.
    """
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("ExecStart="):
            continue
        value = stripped.split("=", 1)[1].strip()
        # systemd prefixes such as "-", "@", "+", "!" are not part of the path.
        value = value.lstrip("-@+!:").strip()
        first = value.split()[0] if value.split() else ""
        if first.startswith("/") and "python" in first.rsplit("/", 1)[-1]:
            return first
    return None


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
        return None, "systemd does not know unit %s (LoadState=%s)" % (
            unit,
            load_state.strip() or "unknown",
        )

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


def unit_in_scope(unit: str) -> tuple[bool | None, str | None, str]:
    """Whether ``unit``'s role actually applies to this host.

    Measured against the real fleet (chiap01, chiap02, chiap03, chiap08)
    before this function existed: ``systemctl --user is-enabled`` reports the
    identical "static" (service) / "disabled" (timer) pair on all four hosts
    for skfleet-rotate.service and skfleet-rotate.timer, so enablement state
    alone cannot tell a rotate host from a seat host. The signal that
    actually differs is runtime activity: skfleet-rotate.timer's ActiveState
    is "active" on chiap01/02/03 and "inactive" on chiap08. A oneshot
    ``.service`` itself reads "inactive" between runs on every host,
    regardless of whether its timer is live, so it is the *timer*, not the
    service, that has to be asked.

    For a ``.service`` unit this checks its paired ``.timer``. For any other
    unit kind it checks the unit itself. A unit is in scope when its
    ActiveState is "active", or its UnitFileState (systemd's own "enabled"
    vocabulary, read via ``show`` for the same reason
    ``systemd_effective_environment`` reads Environment that way rather than
    parsing a file) is "enabled" or "enabled-runtime" -- covering a host that
    is correctly enabled at boot but has not ticked yet.

    Returns:
        ``(in_scope, None, checked_unit)`` when determined, or
        ``(None, message, checked_unit)`` when it could not be. Never raises.
        An undeterminable scope is never treated as out of scope: "I could
        not check" and "I checked and it does not apply here" are different
        facts, so the caller must FAIL, not skip, when this returns an error.
    """
    checked_unit = unit
    if unit.endswith(".service"):
        checked_unit = unit[: -len(".service")] + ".timer"

    load_state, err = _systemctl_show_value(checked_unit, "LoadState")
    if err is not None:
        return None, err, checked_unit
    if load_state.strip() in ("", "not-found"):
        return (
            None,
            "systemd does not know unit %s (LoadState=%s)"
            % (checked_unit, load_state.strip() or "unknown"),
            checked_unit,
        )

    active_state, err = _systemctl_show_value(checked_unit, "ActiveState")
    if err is not None:
        return None, err, checked_unit

    enabled_state, err = _systemctl_show_value(checked_unit, "UnitFileState")
    if err is not None:
        return None, err, checked_unit

    in_scope = active_state.strip() == "active" or enabled_state.strip() in (
        "enabled",
        "enabled-runtime",
    )
    return in_scope, None, checked_unit


def write_verdict(path: Path, ok: bool, lines: list[str]) -> None:
    """Write the gate's verdict as JSON, atomically (tmp file, then
    ``os.replace``), so a reader never observes a half-written file: it is
    Task 3's drift detection and Task 4's reporting that read this path, and
    neither may see a file that is mid-write.

    Args:
        path: Destination file. Parent directories are created as needed.
        ok: The gate's overall READY / NOT READY verdict.
        lines: Every line the gate printed, in order, so a reader gets the
            same detail a human running the gate by hand would see.
    """
    payload = {
        "ready": ok,
        "checked_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "lines": lines,
    }
    data = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8") + b"\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _iter_unit_files(units_dir: Path):
    for path in sorted(units_dir.glob("*.service")):
        yield path


def _run(
    rotate_script: Path,
    units_dir: Path,
    python_bin: str,
    env_from_unit: str | None,
    env_from_systemd: str | None = None,
    verdict_path: Path | None = None,
):
    lines: list[str] = []
    ok = True

    try:
        dispatcher_source = rotate_script.read_text(encoding="utf-8")
    except OSError as exc:
        lines.append("FAIL dispatcher source: could not read %s (%s)" % (rotate_script, exc))
        _write_verdict_if_requested(verdict_path, False, lines)
        print("\n".join(lines))
        print("NOT READY")
        return 1

    mandatory = required_env(dispatcher_source)

    # env_from_systemd names a *role* unit (skfleet-rotate.service): the
    # host this gate is running on may simply not carry that role. Checking
    # that unit's environment is only meaningful when the role applies here,
    # so scope is resolved first, before spending a systemctl round trip on
    # the environment itself. See unit_in_scope's docstring for why this is
    # active-state, not enabled-state: measured against the real fleet,
    # enabled-state does not distinguish a rotate host from a seat host.
    scope_error: str | None = None
    in_scope = True
    checked_scope_unit: str | None = None
    if env_from_systemd:
        in_scope, scope_error, checked_scope_unit = unit_in_scope(env_from_systemd)

    env_error: str | None = None
    if scope_error is not None:
        ok = False
        lines.append(
            "FAIL required env: could not determine whether %s applies to this host "
            "(checked %s: %s); an undetermined scope is never treated as skippable"
            % (env_from_systemd, checked_scope_unit, scope_error)
        )
        for name in sorted(mandatory):
            lines.append(
                "FAIL required env: %s could not be verified (scope of %s unavailable)"
                % (name, env_from_systemd)
            )
    elif env_from_systemd and not in_scope:
        lines.append(
            "SKIP required env: %s is not active on this host (checked %s); "
            "this role does not apply here, its environment was not asserted"
            % (env_from_systemd, checked_scope_unit)
        )
        if not mandatory:
            lines.append(
                "SKIP required env: dispatcher declares no mandatory env vars to check anyway"
            )
        for name in sorted(mandatory):
            lines.append(
                "SKIP required env: %s not checked (%s is not active on this host)"
                % (name, env_from_systemd)
            )
    else:
        if env_from_systemd:
            env_snapshot, env_error = systemd_effective_environment(env_from_systemd)
            env_source_label = "systemd effective environment for unit %s" % env_from_systemd
        elif env_from_unit:
            unit_path = units_dir / env_from_unit
            env_snapshot = _env_from_unit_file(unit_path)
            env_source_label = (
                "unit %s (Environment= lines only, drop-ins not read)" % env_from_unit
            )
        else:
            env_snapshot = dict(os.environ)
            env_source_label = "process environment"

        if env_error is not None:
            ok = False
            lines.append(
                "FAIL required env: could not determine %s (%s); "
                "an undetermined environment is never treated as ready"
                % (env_source_label, env_error)
            )
            for name in sorted(mandatory):
                lines.append(
                    "FAIL required env: %s could not be verified (environment source unavailable)"
                    % name
                )
        else:
            if not mandatory:
                lines.append("OK required env: dispatcher declares no mandatory env vars")
            for name in sorted(mandatory):
                if env_snapshot is not None and name in env_snapshot and env_snapshot[name] != "":
                    lines.append(
                        "OK required env: %s is set (checked against %s)"
                        % (name, env_source_label)
                    )
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

    # Group by the interpreter each unit actually declares, so a unit with its
    # own virtualenv is checked against that venv rather than against whatever
    # --python-bin happens to be. Checking everything against one interpreter
    # fails any unit that legitimately lives elsewhere.
    by_interpreter: dict[str, list[str]] = {}
    for unit_path in unit_files:
        try:
            unit_text = unit_path.read_text(encoding="utf-8")
        except OSError:
            continue
        interpreter = unit_python(unit_text) or python_bin
        for module in unit_modules(unit_text):
            bucket = by_interpreter.setdefault(interpreter, [])
            if module not in bucket:
                bucket.append(module)

    import_results: dict[tuple[str, str], bool] = {}
    for interpreter, modules_for in by_interpreter.items():
        for module, result in check_module_imports(modules_for, interpreter).items():
            import_results[(interpreter, module)] = result

    for unit_path in unit_files:
        try:
            unit_text = unit_path.read_text(encoding="utf-8")
        except OSError:
            continue
        modules = unit_modules(unit_text)
        if not modules:
            continue
        interpreter = unit_python(unit_text) or python_bin
        for module in modules:
            imports_ok = import_results.get((interpreter, module), False)
            if imports_ok:
                lines.append(
                    "OK unit %s: module %s imports under %s"
                    % (unit_path.name, module, interpreter)
                )
            else:
                ok = False
                lines.append(
                    "FAIL unit %s: module %s does not import under %s; "
                    "ExecStart will fail at runtime" % (unit_path.name, module, interpreter)
                )

    ok = _write_verdict_if_requested(verdict_path, ok, lines)

    print("\n".join(lines))
    print("READY" if ok else "NOT READY")
    return 0 if ok else 1


def _write_verdict_if_requested(verdict_path: Path | None, ok: bool, lines: list[str]) -> bool:
    """Persist the verdict when a path was given, and return the (possibly
    revised) overall verdict.

    A write failure is not swallowed behind a silently-correct-looking exit
    code: it is appended to ``lines`` (mutated in place, so the caller's
    printed output carries it) and flips the returned verdict to False, on
    the reasoning that a verdict nobody downstream can read is not a verdict
    a rollout should trust, regardless of what the gate itself found.
    """
    if verdict_path is None:
        return ok
    try:
        write_verdict(verdict_path, ok, lines)
        return ok
    except OSError as exc:
        lines.append("FAIL verdict: could not write %s (%s)" % (verdict_path, exc))
        return False


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
        remainder = stripped[len("Environment=") :].strip()
        if remainder.startswith('"') and remainder.endswith('"'):
            remainder = remainder[1:-1]
        if "=" in remainder:
            key, _, value = remainder.partition("=")
            env[key.strip()] = value.strip()
    return env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fleet readiness gate for a node before rollout continues."
    )
    parser.add_argument(
        "--rotate-script",
        required=True,
        help="Path to the dispatcher source, e.g. scripts/fleet/skfleet-rotate.py",
    )
    parser.add_argument(
        "--units-dir", required=True, help="Directory containing systemd *.service unit files"
    )
    parser.add_argument(
        "--python-bin", required=True, help="Python interpreter to check module imports against"
    )
    parser.add_argument(
        "--env-from-unit",
        default=None,
        help="Optional unit file name; check env against its Environment= lines only (drop-ins are not read), instead of the process environment",
    )
    parser.add_argument(
        "--env-from-systemd",
        default=None,
        help="Optional systemd unit name; check env against the unit's EFFECTIVE environment as reported by systemctl --user show, which already merges the unit file and every drop-in. Preferred over --env-from-unit when both are given. When given, the unit's role is scoped first: its paired timer (for a .service) or the unit itself must be active or boot-enabled on this host, or its environment is reported skipped rather than asserted.",
    )
    parser.add_argument(
        "--verdict-path",
        default=None,
        help="Optional path to write the verdict as JSON (atomic write), so another process can read it without re-running the gate. Not written when omitted.",
    )
    args = parser.parse_args(argv)

    return _run(
        Path(args.rotate_script),
        Path(args.units_dir),
        args.python_bin,
        args.env_from_unit,
        args.env_from_systemd,
        Path(args.verdict_path) if args.verdict_path else None,
    )


if __name__ == "__main__":
    sys.exit(main())
