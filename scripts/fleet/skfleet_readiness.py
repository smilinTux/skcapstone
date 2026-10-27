#!/home/skuser01/.skenv/bin/python
"""Fleet readiness gate.

Before a rollout goes to more than one host, this checks that a node has
everything the dispatcher will need at runtime: every mandatory environment
variable the dispatcher requires, and every module every systemd unit's
ExecStart names, actually importable under the target Python.

Two things went wrong in production that this exists to catch:
  1. A node came up FAILING because main required SKFLEET_GATEWAY_URL and no
     systemd unit supplied it.
  2. A unit named a module (skcapstone.seat_shadow_entrypoint) that did not exist
     on main. Nothing caught a unit pointing at a missing
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
from typing import NamedTuple


def required_env(source: str, *, production: bool = False) -> set[str]:
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
            if production:
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

    if production:
        names.discard("SKFLEET_GATEWAY_URL")
        names.update({"SKFLEET_PRODUCTION_POLICY", "SKFLEET_AUTHORITY_HOST"})
    return names


def production_environment_error(
    environment: dict[str, str],
    python_bin: str,
    dispatcher: Path,
) -> str | None:
    """Validate policy with the actual native interpreter; never execute dispatch."""
    code = (
        "import ast,json,socket,sys; from pathlib import Path; "
        "from skcapstone.fleet.production_dispatch import production_policy_from_environment; "
        "from skcapstone.fleet.production_policy import LEGACY_CEILING_VARIABLES; "
        "payload=json.load(sys.stdin); environment=payload['environment']; "
        "host=socket.gethostname().split('.')[0].lower()\n"
        "if environment.get('SKFLEET_AUTHORITY_HOST') != host: "
        "raise ValueError('effective authority mismatch')\n"
        "if not environment.get('SKFLEET_PRODUCTION_POLICY'): "
        "raise ValueError('explicit policy required')\n"
        "if LEGACY_CEILING_VARIABLES.intersection(environment): "
        "raise ValueError('production policy refuses legacy local ceilings')\n"
        "tree=ast.parse(Path(payload['dispatcher']).read_text(encoding='utf-8'))\n"
        "markers=[n for n in tree.body if isinstance(n, ast.Assign) "
        "and any(isinstance(t, ast.Name) and t.id == 'SKFLEET_PRODUCTION_POLICY_V1' "
        "for t in n.targets)]\n"
        "if (len(markers) != 1 or len(markers[0].targets) != 1 "
        "or not isinstance(markers[0].value, ast.Constant) "
        "or markers[0].value.value is not True): "
        "raise ValueError('dispatcher lacks production policy capability')\n"
        "policy=production_policy_from_environment(environment,host)\n"
        "if not policy: raise ValueError('explicit production policy required')\n"
    )
    try:
        result = subprocess.run(
            [python_bin, "-c", code],
            input=json.dumps({"environment": environment, "dispatcher": str(dispatcher)}),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "native production policy validation unavailable"
    if result.returncode != 0:
        return "invalid production policy or effective authority"
    return None


def qualification_sandbox_applies() -> bool:
    """Whether this host's node role runs qualified work (builder-standby or control).

    Keyed on the admitted node role, not on any paused dispatcher unit. A node
    with no admitted spec or role does not run qualified work (False). A spec
    that exists but cannot be read answers True: an unreadable role must not
    silently drop the sandbox gate.
    """
    try:
        import socket

        from skcapstone.fleet import store
        from skcapstone.fleet.paths import default_paths

        node = "node-" + socket.gethostname().split(".")[0].lower()
        spec = store.read_spec(default_paths(), "node", node)
        role = ((spec or {}).get("spec") or {}).get("role")
    except Exception:
        return True
    return role in ("builder-standby", "control")


def qualification_python() -> str:
    """Sealed tests run from the clean qualification prefix, not the dispatcher's."""
    from skcapstone.fleet.qualified_runtime import qualify_prefix

    return str(qualify_prefix(Path.home()) / "bin/python")


def qualification_sandbox_error(python_bin: str) -> str | None:
    """Grade worker context, preserving readiness's nested filesystem sandbox."""
    import uuid

    command = [
        "/usr/bin/systemd-run",
        "--user",
        "--quiet",
        "--pipe",
        "--wait",
        "--collect",
        "--unit=skfleet-sandbox-probe-" + uuid.uuid4().hex,
        "--property=RuntimeMaxSec=30",
        "--property=MemoryMax=256M",
        "--property=CPUQuota=100%",
        "--property=TasksMax=64",
        "--property=UnsetEnvironment=BASH_ENV",
        "--setenv=PATH=/usr/bin:/bin",
        "--",
        python_bin,
        "-I",
        "-m",
        "skcapstone.fleet.production_sandbox_probe",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=40,
            env={key: value for key, value in os.environ.items() if key != "BASH_ENV"},
        )
    except (OSError, subprocess.SubprocessError):
        return "native sealed sandbox unavailable or timed out"
    try:
        if len(result.stdout) > 8192:
            raise ValueError("probe result exceeds bound")
        value = json.loads(result.stdout)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "failures"}
            or value["schema"] != "skfleet.sandbox-readiness/v1"
            or not isinstance(value["failures"], list)
            or len(value["failures"]) > 20
            or any(not isinstance(f, str) or not 1 <= len(f) <= 1024 for f in value["failures"])
        ):
            raise ValueError("invalid probe result")
        if value["failures"]:
            return "; ".join(value["failures"])
    except (ValueError, TypeError):
        return "native sealed sandbox diagnostic missing or invalid"
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        return "native sealed sandbox refused" + (": " + detail[0][:240] if detail else "")
    return None


class ExecTarget(NamedTuple):
    """One ``python -m <module>`` that a unit's ExecStart runs, resolved the way
    systemd would resolve it.

    interpreter is an absolute path, or None when ExecStart names a bare
    ``python3`` that PATH could not resolve (the caller then uses its own
    --python-bin, which is the historical behaviour). cwd is the unit's
    WorkingDirectory: ``python -m`` puts the working directory first on
    sys.path, so a service that runs its package from a source tree only
    imports from there. env holds the unit's PYTHON* variables. unresolved is
    set when the interpreter or cwd depends on something this gate cannot
    know (a template instance, an undefined variable), so the target cannot be
    checked honestly in either direction.
    """

    module: str
    interpreter: str | None
    cwd: str | None
    env: dict
    unresolved: str | None = None


PYTHON_NAME_RE = re.compile(r"^python(\d+(\.\d+)*)?$")
MODULE_NAME_RE = re.compile(r"^[A-Za-z_][\w.]*$")
SHELL_NAMES = {"sh", "bash", "dash", "zsh"}
# Interpreter options that take a separate argument (python -X dev -m pkg).
PYTHON_OPTS_WITH_ARG = {"-X", "-W", "-Q"}
SHELL_SEPARATORS = {"&&", "||", ";", "|"}


def _directive_values(unit_text: str, key: str) -> list[str]:
    prefix = key + "="
    return [
        line.strip()[len(prefix) :].strip()
        for line in unit_text.splitlines()
        if line.strip().startswith(prefix)
    ]


def expand_specifiers(value: str, unit_name: str, home: str) -> tuple[str, list[str]]:
    """Expand the systemd unit specifiers this gate can know (%h %u %U %t %H %n
    %N %p %i %I %%). Returns the expanded text and the specifiers it could not
    expand; %i/%I are unknowable on a template file (``name@.service``)."""
    stem = unit_name[: -len(".service")] if unit_name.endswith(".service") else unit_name
    prefix, at, instance = stem.partition("@")
    mapping = {
        "h": home,
        "u": os.environ.get("USER") or Path(home).name,
        "U": str(os.getuid()),
        "t": os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(),
        "H": os.uname().nodename,
        "%": "%",
        "p": prefix,
    }
    if not (at and not instance):
        mapping.update({"n": unit_name, "N": stem, "i": instance, "I": instance})
    missing: list[str] = []

    def repl(match: re.Match) -> str:
        spec = match.group(1)
        if spec in mapping:
            return mapping[spec]
        missing.append("%" + spec)
        return match.group(0)

    return re.sub(r"%(.)", repl, value), missing


def _expand_env(value: str, env: dict) -> tuple[str, list[str]]:
    missing: list[str] = []

    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        if name in env:
            return env[name]
        missing.append("$" + name)
        return match.group(0)

    return re.sub(r"\$\{(\w+)\}|\$([A-Za-z_]\w*)", repl, value), missing


def _parse_env_assignments(text: str) -> dict:
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    return dict(t.partition("=")[::2] for t in tokens if "=" in t)


def unit_environment(unit_text: str, unit_name: str, home: str) -> dict:
    """The variables a unit defines for its own ExecStart: Environment= lines,
    then EnvironmentFile= files (which override them, as in systemd). A file
    that is optional, missing, or names an unknowable specifier is skipped."""
    env: dict = {}
    for raw in _directive_values(unit_text, "Environment"):
        env.update(_parse_env_assignments(raw))
    for raw in _directive_values(unit_text, "EnvironmentFile"):
        path, missing = expand_specifiers(raw.lstrip("-").strip(), unit_name, home)
        if missing:
            continue
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, eq, val = line.partition("=")
            if eq:
                env[key.strip()] = val.strip().strip("'\"")
    return env


def _resolve_interpreter(token: str, env: dict, home: str) -> str | None:
    if token.startswith("~"):
        token = home + token[1:]
    if token.startswith("/"):
        return token
    if "PATH" in env:
        import shutil

        return shutil.which(token, path=env["PATH"])
    return None


def _scan_tokens(tokens: list[str], env: dict, home: str, cwd: str | None, found: list):
    """Find every python [-opts] -m <module> in a command line, descending into
    ``env ...`` and ``sh -c '...'`` wrappers and following ``cd DIR``."""
    while tokens:
        base = tokens[0].rsplit("/", 1)[-1]
        if base == "env":
            tokens = tokens[1:]
            while tokens and (tokens[0].startswith("-") or "=" in tokens[0]):
                tokens = tokens[1:]
            continue
        if base in SHELL_NAMES:
            for i, tok in enumerate(tokens[1:], 1):
                if tok.startswith("-") and not tok.startswith("--") and "c" in tok:
                    if i + 1 < len(tokens):
                        try:
                            inner = shlex.split(tokens[i + 1])
                        except ValueError:
                            inner = tokens[i + 1].split()
                        _scan_tokens(inner, env, home, cwd, found)
                    return
            return
        break
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "cd" and i + 1 < len(tokens):
            target = tokens[i + 1]
            cwd = home + target[1:] if target.startswith("~") else target
            i += 2
            continue
        if PYTHON_NAME_RE.match(tok.rsplit("/", 1)[-1]):
            j = i + 1
            while j < len(tokens) and tokens[j].startswith("-") and tokens[j] != "-m":
                j += 2 if tokens[j] in PYTHON_OPTS_WITH_ARG else 1
            if j + 1 < len(tokens) and tokens[j] == "-m" and MODULE_NAME_RE.match(tokens[j + 1]):
                found.append((tok, tokens[j + 1], cwd))
                i = j + 2
                continue
        i += 1


def unit_exec_targets(unit_text: str, unit_name: str = "unit.service", home: str | None = None):
    """Every module a unit's ExecStart lines run, each with the interpreter,
    working directory and PYTHON* env it will actually run under.

    Regression this exists for (2026-10-25 to 10-27): the chi auto-rollout
    halted at chiap01 on every cycle because sklipsync.service runs
    ``ExecStart=%h/skcall-lipsync-bench/venv/bin/python -m sklipsync serve``
    from ``WorkingDirectory=%h/skcall-lipsync/...``. The old parser only
    accepted an interpreter path starting with "/", so the ``%h`` path fell
    back to ~/.skenv's python, where sklipsync is not installed; and it ran
    the import from the gate's own cwd, where the source tree is not on
    sys.path. skcall-join@.service on chiap04 (``%h/.skenv-skcall``) failed
    the same way. Every host after chiap01 sat on an old main for days.
    """
    home = home or str(Path.home())
    env = unit_environment(unit_text, unit_name, home)
    python_env = {k: v for k, v in env.items() if k.startswith("PYTHON")}
    unresolved_cwd = None
    cwd = home
    workdirs = _directive_values(unit_text, "WorkingDirectory")
    if workdirs:
        raw = workdirs[-1].lstrip("-").strip()
        if raw == "~":
            cwd = home
        elif raw:
            cwd, missing = expand_specifiers(raw, unit_name, home)
            if missing:
                unresolved_cwd = "WorkingDirectory needs %s" % ", ".join(missing)
    targets: list[ExecTarget] = []
    seen: set = set()
    for raw in _directive_values(unit_text, "ExecStart"):
        value = raw.lstrip("-@+!:").strip()
        value, missing_spec = expand_specifiers(value, unit_name, home)
        value, missing_env = _expand_env(value, env)
        try:
            tokens = shlex.split(value)
        except ValueError:
            tokens = value.split()
        found: list = []
        _scan_tokens(tokens, env, home, cwd, found)
        for interp_token, module, target_cwd in found:
            unresolved = None
            bad = [m for m in missing_spec + missing_env if m in interp_token]
            if bad:
                unresolved = "interpreter %s needs %s" % (interp_token, ", ".join(bad))
            elif target_cwd == cwd and unresolved_cwd:
                unresolved = unresolved_cwd
            interpreter = None if bad else _resolve_interpreter(interp_token, env, home)
            key = (module, interpreter, target_cwd)
            if key in seen:
                continue
            seen.add(key)
            targets.append(ExecTarget(module, interpreter, target_cwd, python_env, unresolved))
    return targets


def unit_modules(unit_text: str) -> list[str]:
    """Return every ``python -m <module>`` module path found in ExecStart=
    lines, in order, with no duplicates."""
    modules: list[str] = []
    for target in unit_exec_targets(unit_text):
        if target.module not in modules:
            modules.append(target.module)
    return modules


def unit_python(unit_text: str, unit_name: str = "unit.service", home: str | None = None):
    """The absolute interpreter a unit's ExecStart names, with systemd
    specifiers such as ``%h`` expanded, or None so the caller falls back to
    the interpreter it was given.

    A unit is entitled to its own virtualenv (measured on chiap04 2026-09-18:
    hermes-gateway.service runs ~/.hermes/hermes-agent/venv/bin/python and
    readiness falsely failed it under ~/.skenv). See unit_exec_targets for the
    %h variant of the same false failure.
    """
    home = home or str(Path.home())
    for raw in _directive_values(unit_text, "ExecStart"):
        value = raw.lstrip("-@+!:").strip()
        value, _ = expand_specifiers(value, unit_name, home)
        first = value.split()[0] if value.split() else ""
        if first.startswith("/") and "python" in first.rsplit("/", 1)[-1]:
            return first
    return None


def dispatcher_imports(source: str) -> list[str]:
    """Return every top-level module the dispatcher SCRIPT imports.

    unit_modules() only recognises ``python -m <module>`` ExecStart lines, so a
    unit whose ExecStart names a SCRIPT PATH gets no import verification at all.
    That is skfleet-rotate.service, the single most important unit in the fleet.

    Measured 2026-09-21: chiap02 crashed at import on EVERY cycle for hours
    (``ImportError: cannot import name 'AuthorizationDecision' from 'capauth'``,
    capauth 0.3.1 against the fleet's 0.3.9+) while this gate reported only that
    the unit's environment variables were set. Eleven cards sat owned by a host
    that could not start its dispatcher, and the gate said nothing about it.

    Only top-level imports are returned. An import inside a function is not
    startup-critical and is frequently guarded on purpose.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    modules: list[str] = []
    seen: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # A relative import cannot be resolved standalone, and the script is
            # not part of a package, so there should never be one here.
            if node.level or not node.module:
                continue
            names = [node.module]
        else:
            continue
        for name in names:
            if name not in seen:
                seen.add(name)
                modules.append(name)
    return modules


def check_module_imports(
    modules: list[str],
    python_bin: str,
    *,
    cwd: str | None = None,
    env: dict | None = None,
) -> dict[str, bool]:
    """Return {module: True/False} for whether each imports under python_bin,
    run from cwd with env layered over this process's environment (a unit's
    WorkingDirectory and PYTHON* vars, which decide what ``python -m`` sees).

    Never raises: a subprocess failure of any kind (including a missing
    interpreter or working directory, which would fail the unit too) is
    recorded as False.
    """
    results: dict[str, bool] = {}
    run_env = {**os.environ, **env} if env else None
    for module in modules:
        code = "import importlib; importlib.import_module('%s')" % module
        try:
            proc = subprocess.run(
                [python_bin, "-c", code],
                capture_output=True,
                text=True,
                cwd=cwd,
                env=run_env,
                timeout=180,
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


#: Substring of the unit_in_scope error that means systemd answered "not installed".
NOT_INSTALLED_MARK = "(LoadState=not-found)"


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


_DIST_INFO_GIT_SHA_RE = re.compile(r"\+g([0-9a-f]+)", re.IGNORECASE)


def installed_git_sha():
    """The git commit embedded in the installed skcapstone distribution.

    setuptools_scm bakes the short git hash into the version it derives from
    the git tag, and that version is part of the dist-info directory name
    itself (skcapstone-0.15.168.dev266+g0c8dcd6b.dist-info), so this needs
    only a glob under the one place SK* packages install. No subprocess, no
    file open, no import of skcapstone -- this script is stdlib-only on
    purpose and stays that way.

    Published in the verdict so the estate can answer a question no
    single-node check can: do the nodes agree with EACH OTHER about what
    they are running. Every node already writes this verdict every 15
    minutes to the one Syncthing folder the estate shares, so that question
    becomes readable from any host with no new publishing step. See
    skcapstone.fleet.rollout_drift.detect_fleet_incoherence.

    Deliberately a content identifier and not the semantic version: 0.15.168
    reads identically across unrelated commits, which is exactly what let
    the 2026-09-19 outage hide.

    Returns None (never a guess) when it cannot be read, so a reader can
    tell "this node is on commit X" from "this node could not say", which
    are different facts. This NEVER affects the ready verdict: a node whose
    dist-info cannot be globbed is not thereby unready.
    """
    home = Path(os.path.expanduser("~"))
    # Start below home so glob never stats unrelated entries such as NFS links.
    for dist_info in sorted(
        (home / ".skenv" / "lib").glob("python3.*/site-packages/skcapstone-*.dist-info")
    ):
        match = _DIST_INFO_GIT_SHA_RE.search(dist_info.name)
        if match:
            return match.group(1).lower()
    return None


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
        "installed_git_sha": installed_git_sha(),
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
    env_from_process: bool = False,
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

    sandbox_checked = qualification_sandbox_applies()
    if not sandbox_checked:
        lines.append("SKIP qualification sandbox: node role does not run qualified work")
    else:
        sandbox_error = qualification_sandbox_error(python_bin)
        if sandbox_error:
            ok = False
            lines.append("FAIL qualification sandbox: " + sandbox_error)
        else:
            lines.append("OK qualification sandbox: native sealed execution")

    # Verify the dispatcher script itself actually imports. Its unit names a
    # script path, not `python -m`, so unit_modules() below never covers it.
    dispatcher_modules = dispatcher_imports(dispatcher_source)
    if dispatcher_modules:
        dispatcher_results = check_module_imports(dispatcher_modules, python_bin)
        dispatcher_failed = [
            module for module in dispatcher_modules if not dispatcher_results.get(module)
        ]
        if dispatcher_failed:
            ok = False
            lines.append(
                "FAIL dispatcher import: %s does not import under %s; "
                "%s crashes at startup on every cycle"
                % (", ".join(dispatcher_failed), python_bin, rotate_script)
            )
        else:
            lines.append(
                "OK dispatcher imports: all %d top-level imports of %s resolve under %s"
                % (len(dispatcher_modules), rotate_script, python_bin)
            )

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

    # LoadState=not-found is systemd's authoritative answer that the unit is not
    # installed (a paused or removed role). That is "does not apply here", not "could not check", so
    # the gate SKIPs. unit_in_scope itself keeps reporting it as undetermined
    # because rollout_drift relies on that to flag manifest units that are
    # missing from a host. An empty or failed query stays a FAIL.
    if scope_error is not None and NOT_INSTALLED_MARK in scope_error:
        in_scope, scope_error = False, None

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
    elif not (env_from_systemd or env_from_unit or env_from_process):
        # No environment source was named. The dispatcher (skfleet-rotate.py)
        # is dormant and no live unit carries its environment, so there is
        # nothing to assert; imports of the script and of every unit are still
        # checked below. A host or test that wants the process environment
        # asserted names it explicitly with --env-from-process.
        lines.append(
            "SKIP required env: no dispatcher role unit named (dormant dispatcher); "
            "its environment was not asserted"
        )
    elif env_from_systemd and not in_scope:
        lines.append(
            "SKIP required env: %s is not active or not installed on this host (checked %s); "
            "this role does not apply here, its environment was not asserted"
            % (env_from_systemd, checked_scope_unit)
        )
        if not mandatory:
            lines.append(
                "SKIP required env: dispatcher declares no mandatory env vars to check anyway"
            )
        for name in sorted(mandatory):
            lines.append(
                "SKIP required env: %s not checked (%s is not active or not installed on this host)"
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
            if env_snapshot is not None and ("SKFLEET_PRODUCTION_POLICY" in env_snapshot):
                policy_error = production_environment_error(
                    env_snapshot, python_bin, rotate_script
                )
                if policy_error:
                    ok = False
                    lines.append("FAIL production policy: " + policy_error)
                else:
                    mandatory = required_env(dispatcher_source, production=True)
                    lines.append(
                        "OK production policy: native parser and effective authority validated"
                    )
                    if not sandbox_checked:
                        sandbox_error = qualification_sandbox_error(python_bin)
                        if sandbox_error:
                            ok = False
                            lines.append("FAIL qualification sandbox: " + sandbox_error)
                        else:
                            lines.append("OK qualification sandbox: native sealed execution")
                    from skcapstone.fleet.qualified_runtime import REQUIRED_PACKAGES

                    qualified = check_module_imports(
                        list(REQUIRED_PACKAGES), qualification_python()
                    )
                    for module in REQUIRED_PACKAGES:
                        if not qualified.get(module):
                            ok = False
                            lines.append("FAIL qualified-runtime dependency: " + module)
                    if all(qualified.get(module) for module in REQUIRED_PACKAGES):
                        lines.append("OK qualified-runtime dependencies: qualify-env")
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

    unit_files = list(_iter_unit_files(units_dir))
    if not unit_files:
        lines.append("FAIL units: no *.service files found under %s" % units_dir)
        ok = False
    home = str(Path.home())
    import_cache: dict = {}
    for unit_path in unit_files:
        try:
            unit_text = unit_path.read_text(encoding="utf-8")
        except OSError as exc:
            ok = False
            lines.append("FAIL unit %s: could not read file (%s)" % (unit_path.name, exc))
            continue
        # Each module is checked the way the unit runs it: under the
        # interpreter its own ExecStart names (%h and $VARS expanded), from its
        # WorkingDirectory, with its PYTHON* env. A unit is entitled to its own
        # virtualenv; checking every unit against --python-bin fails any unit
        # that legitimately lives elsewhere. Units that run no python -m
        # (binaries, wrapper scripts) have nothing to import-check here.
        for target in unit_exec_targets(unit_text, unit_path.name, home):
            if target.unresolved:
                lines.append(
                    "SKIP unit %s: module %s not checked; %s, which this gate cannot know"
                    % (unit_path.name, target.module, target.unresolved)
                )
                continue
            interpreter = target.interpreter or python_bin
            where = (
                interpreter if target.cwd == home else "%s (cwd %s)" % (interpreter, target.cwd)
            )
            key = (interpreter, target.cwd, tuple(sorted(target.env.items())), target.module)
            if key not in import_cache:
                result = check_module_imports(
                    [target.module], interpreter, cwd=target.cwd, env=target.env
                )
                import_cache[key] = bool(result.get(target.module))
            if import_cache[key]:
                lines.append(
                    "OK unit %s: module %s imports under %s"
                    % (unit_path.name, target.module, where)
                )
            else:
                ok = False
                lines.append(
                    "FAIL unit %s: module %s does not import under %s; "
                    "ExecStart will fail at runtime" % (unit_path.name, target.module, where)
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
        help=(
            "Optional unit file name; check env against its Environment= lines only (drop-ins "
            "are not read), instead of the process environment"
        ),
    )
    parser.add_argument(
        "--env-from-systemd",
        default=None,
        help=(
            "Optional systemd unit name; check env against the unit's EFFECTIVE environment "
            "as reported by systemctl --user show, which already merges the unit file and "
            "every drop-in. Preferred over --env-from-unit when both are given. When given, "
            "the unit's role is scoped first: its paired timer (for a .service) or the unit "
            "itself must be active or boot-enabled on this host, or its environment is "
            "reported skipped rather than asserted."
        ),
    )
    parser.add_argument(
        "--env-from-process",
        action="store_true",
        help=(
            "Assert the dispatcher's mandatory env against this process's own environment. "
            "With none of the three env sources given, the env check is skipped because the "
            "dispatcher is dormant and no live unit carries its environment."
        ),
    )
    parser.add_argument(
        "--verdict-path",
        default=None,
        help=(
            "Optional path to write the verdict as JSON (atomic write), so another process "
            "can read it without re-running the gate. Not written when omitted."
        ),
    )
    args = parser.parse_args(argv)

    return _run(
        Path(args.rotate_script),
        Path(args.units_dir),
        args.python_bin,
        args.env_from_unit,
        args.env_from_systemd,
        Path(args.verdict_path) if args.verdict_path else None,
        args.env_from_process,
    )


if __name__ == "__main__":
    sys.exit(main())
