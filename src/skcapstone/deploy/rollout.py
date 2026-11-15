"""Roll a node onto a release manifest, and back off it.

The order is fixed and every step can stop the rollout before production
changes:

1. pre-flight: every pinned release is a tag on its repository's default
   branch (a version that is not on main is refused, not warned about);
2. build a NEW venv next to the live one, non-editable, from the manifest;
3. verify that venv in place (imports, console-script targets, ``pip check``,
   no executable lost from ``bin/`` compared with the live venv);
3b. export the manifest's tools from main and point units at them (see
   :mod:`skcapstone.deploy.tools`);
4. switch the deploy path (``~/.skenv``) to it with one atomic rename;
5. restart only the units whose packages changed;
6. smoke-test; on any failure switch back and restart the same units.

State is host-local (``~/.local/state/sk-deploy``), never under the
Syncthing-shared ``~/.skcapstone``: one node's deploy is not another's.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import files as _files
from .files import _atomic_write
from .files import sha256 as _sha_bytes
from .inspect import default_unit_dir, list_dists, normalize
from .manifest import Manifest
from .tools import (
    DROPIN,
    ToolError,
    ToolsChange,
    apply_tools,
    default_root,
    displaced_blocker,
    restart_targets,
)

Runner = Callable[..., subprocess.CompletedProcess]

#: Runs inside the NEW venv: import every console-script target and named module.
_VERIFY_SCRIPT = r"""
import importlib, importlib.metadata as md, json, sys
want = json.loads(sys.argv[1])
bad = []
for mod in want["imports"]:
    try:
        importlib.import_module(mod)
    except Exception as exc:
        bad.append(f"import {mod}: {type(exc).__name__}: {exc}")
pkgs = set(want["packages"])
import re
norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()
for d in md.distributions():
    if norm(d.metadata["Name"] or "") not in pkgs:
        continue
    for ep in d.entry_points:
        if ep.group != "console_scripts":
            continue
        try:
            ep.load()
        except Exception as exc:
            bad.append(f"entry point {ep.name} ({ep.value}): {type(exc).__name__}: {exc}")
print(json.dumps(bad))
"""


class DeployError(RuntimeError):
    """A rollout step refused or failed; production was not switched."""


@dataclass
class Paths:
    """Where a node keeps its deploy state."""

    venv_link: Path
    releases: Path
    state_dir: Path
    tools_root: Optional[Path] = None

    @classmethod
    def default(cls, home: Optional[Path] = None) -> "Paths":
        """The standard layout under *home*."""
        home = home or Path.home()
        return cls(
            venv_link=home / ".skenv",
            releases=home / ".skenv-releases",
            state_dir=home / ".local" / "state" / "sk-deploy",
            tools_root=home / ".local" / "share" / "sk-tools",
        )

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"


@dataclass
class Plan:
    """What a rollout would change on this node."""

    changed: dict[str, tuple[Optional[str], str]] = field(default_factory=dict)
    editable: list[str] = field(default_factory=list)
    restarts: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changed and not self.editable


def _run(cmd: list[str], runner: Runner, timeout: int = 600, **kw) -> subprocess.CompletedProcess:
    return runner(cmd, capture_output=True, text=True, timeout=timeout, check=False, **kw)


def load_state(paths: Paths) -> dict:
    """The node's deploy state (current and previous release)."""
    try:
        return json.loads(paths.state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(paths: Paths, state: dict) -> None:
    """Write the deploy state atomically."""
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, paths.state_file)


# --------------------------------------------------------------------------- plan


def make_plan(manifest: Manifest, live_python: Path, node: Optional[str] = None) -> Plan:
    """Diff the live venv against the manifest.

    Args:
        manifest: The release set.
        live_python: The live venv's interpreter.
        node: Node profile to use (this host by default).

    Returns:
        The changed packages, the editable ones, and the units to restart.
    """
    plan = Plan()
    if not manifest.packages_for(node):
        return plan  # a tools-only node has no venv to compare
    installed = {normalize(d.name): d for d in list_dists(live_python)}
    for name, spec in manifest.packages_for(node).items():
        have = installed.get(normalize(name))
        want = spec.version or str(spec.ref)
        if spec.source == "editable":
            if have is None or not have.editable:
                plan.changed[name] = (have.version if have else None, "editable")
            continue
        if have is None or have.editable or (spec.source == "pypi" and have.version != want):
            plan.changed[name] = (have.version if have else None, want)
        if have is not None and have.editable:
            plan.editable.append(name)
    changed = {normalize(n) for n in plan.changed}
    for unit, svc in manifest.services_for(node).items():
        if any(normalize(p) in changed for p in svc.packages):
            plan.restarts.append(unit)
    return plan


# ---------------------------------------------------------------------- preflight


def preflight(
    manifest: Manifest, runner: Runner = subprocess.run, node: Optional[str] = None
) -> None:
    """Refuse any pinned release that is not a tag on its default branch.

    Uses the GitHub compare API: the tag must be ``identical`` to or
    ``behind`` the default branch, which means the tag's commit is on it.

    Raises:
        DeployError: One or more releases are not on the default branch.
    """
    problems: list[str] = []
    for name, spec in manifest.packages_for(node).items():
        if spec.source == "editable":
            continue  # a declared exception; drift keeps reporting it
        tag = spec.release_tag()
        proc = _run(
            ["gh", "api", f"repos/{spec.repo}/compare/{spec.branch}...{tag}", "--jq", ".status"],
            runner,
            timeout=60,
        )
        status = proc.stdout.strip()
        if proc.returncode != 0:
            problems.append(f"{name}: tag {tag} not found in {spec.repo}")
        elif status not in ("identical", "behind"):
            problems.append(f"{name}: tag {tag} is {status} {spec.branch}, not on it")
    if problems:
        raise DeployError("pre-flight refused: " + "; ".join(problems))


# -------------------------------------------------------------------------- build


def build_venv(
    manifest: Manifest,
    dest: Path,
    python: str,
    runner: Runner = subprocess.run,
    node: Optional[str] = None,
) -> None:
    """Create *dest* as a fresh, non-editable venv of the manifest.

    Raises:
        DeployError: uv failed.
    """
    if dest.exists():
        raise DeployError(f"{dest} already exists; refusing to build over it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    reqs = dest.parent / f".{dest.name}.requirements.txt"
    reqs.write_text("\n".join(manifest.requirement_lines(node)) + "\n", encoding="utf-8")
    cons = dest.parent / f".{dest.name}.constraints.txt"
    cons.write_text("\n".join(manifest.constraints) + "\n", encoding="utf-8")
    try:
        proc = _run(["uv", "venv", "--seed", "--python", python, str(dest)], runner)
        if proc.returncode != 0:
            raise DeployError(f"uv venv failed: {proc.stderr.strip()[-500:]}")
        proc = _run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(dest / "bin" / "python"),
                "-r",
                str(reqs),
                "-c",
                str(cons),
                *(["--no-deps"] if manifest.lock_for(node) else []),
                # a release is often minutes old: never trust a cached index for SK packages
                *[
                    arg
                    for name in manifest.packages_for(node)
                    for arg in ("--refresh-package", name)
                ],
            ],
            runner,
            timeout=3600,
        )
        if proc.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise DeployError(f"uv pip install failed: {proc.stderr.strip()[-1500:]}")
    finally:
        reqs.unlink(missing_ok=True)
        cons.unlink(missing_ok=True)


def verify_venv(
    manifest: Manifest,
    dest: Path,
    live: Optional[Path],
    runner: Runner = subprocess.run,
    node: Optional[str] = None,
    allow_missing_bin: tuple[str, ...] = (),
) -> list[str]:
    """Check the new venv before anything points at it.

    Returns:
        Problems found; empty means the venv is fit to switch to.
    """
    problems: list[str] = []
    pkgs = manifest.packages_for(node)
    imports = [m for spec_name, spec in pkgs.items() for m in (spec.imports or [])]
    want = {"imports": imports, "packages": [normalize(n) for n in pkgs]}
    proc = _run(
        [str(dest / "bin" / "python"), "-I", "-c", _VERIFY_SCRIPT, json.dumps(want)], runner
    )
    if proc.returncode != 0:
        problems.append(f"verify script failed: {proc.stderr.strip()[-500:]}")
    else:
        problems += json.loads(proc.stdout or "[]")
    new_issues = _pip_check(dest / "bin" / "python", runner)
    old_issues = _pip_check(live / "bin" / "python", runner) if live is not None else set()
    fresh = sorted(new_issues - old_issues)
    if fresh:
        problems.append(f"pip check: {len(fresh)} new problem(s): " + "; ".join(fresh[:20]))
    declared = {normalize(n) for n, spec in pkgs.items() if spec.source == "editable"}
    for dist in list_dists(dest / "bin" / "python"):
        if dist.editable and normalize(dist.name) not in declared:
            problems.append(f"{dist.name} is editable in the new venv")
    if live is not None and (live / "bin").is_dir():
        have = {p.name for p in (dest / "bin").iterdir()}
        lost = sorted(
            p.name
            for p in (live / "bin").iterdir()
            if p.name not in have
            and p.name not in allow_missing_bin
            and not p.name.startswith(("activate", "Activate", "python", "pip", "__pycache__"))
        )
        if lost:
            problems.append(f"bin/ would lose {len(lost)} executable(s): {', '.join(lost)}")
    return problems


def _pip_check(python: Path, runner: Runner) -> set[str]:
    """``uv pip check`` problems as a set of lines (empty when consistent).

    The live venv may already be inconsistent; verification compares against
    it so a rollout is refused only for problems it would INTRODUCE.
    """
    proc = _run(["uv", "pip", "check", "--python", str(python)], runner)
    if proc.returncode == 0:
        return set()
    lines = {ln.strip() for ln in (proc.stdout + proc.stderr).splitlines()}
    noise = ("Checked", "Found", "Resolved", "Using Python")
    return {ln for ln in lines if ln and not ln.startswith(noise)}


def carry_bin(
    manifest: Manifest, live: Optional[Path], dest: Path, node: Optional[str] = None
) -> list[str]:
    """Copy the manifest's hand-placed ``bin/`` files from the live venv.

    Some nodes keep shell shims and symlinks in the deploy venv's ``bin/``
    that no package installs. They are carried verbatim (a symlink stays a
    symlink) so a rollout does not silently remove a command. A packaged
    entry point of the same name always wins.

    Returns:
        The names copied.
    """
    copied: list[str] = []
    if live is None:
        return copied
    for name in manifest.carry_bin_for(node):
        src, dst = live / "bin" / name, dest / "bin" / name
        if dst.exists() or dst.is_symlink() or not (src.exists() or src.is_symlink()):
            continue
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    return copied


# ------------------------------------------------------------------------- switch


def switch(paths: Paths, target: Path) -> Optional[Path]:
    """Point the deploy path at *target* with one atomic rename.

    On the first rollout the deploy path is still a real directory: it is
    moved aside into the releases directory as ``legacy-<date>`` (the
    rollback target) and replaced by the symlink.

    Returns:
        What the deploy path pointed at before (the rollback target).
    """
    link = paths.venv_link
    previous: Optional[Path] = None
    if link.is_symlink():
        previous = Path(os.readlink(link))
    elif link.exists():
        legacy = paths.releases / f"legacy-{time.strftime('%Y%m%d-%H%M%S')}"
        paths.releases.mkdir(parents=True, exist_ok=True)
        os.rename(link, legacy)
        previous = legacy
    tmp = link.with_name(f".{link.name}.switch-{os.getpid()}")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)
    return previous


# ------------------------------------------------------------------ restart/smoke


#: How long a restarted unit has to come back under a NEW invocation. It must
#: cover the unit's stop budget (TimeoutStopSec, 90 s for hermes-gateway) plus
#: its start, or a slow stop reads as a failed deploy.
RESTART_WAIT = 300


def _unit_show(unit: str, runner: Runner) -> dict[str, str]:
    proc = _run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "InvocationID",
            "-p",
            "ActiveEnterTimestamp",
        ],
        runner,
        timeout=30,
    )
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def restart_units(
    units: list[str],
    runner: Runner = subprocess.run,
    *,
    wait: int = RESTART_WAIT,
    started: Optional[dict[str, dict[str, str]]] = None,
) -> list[str]:
    """``try-restart`` each unit (only running units restart) and prove a new start.

    A restart counts only when the unit is ``active`` under a different
    ``InvocationID`` than before: a ``try-restart`` that times out while the old
    process is still stopping, or a unit that never really went down, must not
    pass (hermes-gateway, 2026-11-15, where the smoke then matched the previous
    start's log lines). A unit that reports no InvocationID keeps the plain
    ``is-active`` check.

    Args:
        units: Units to restart.
        runner: Command runner.
        wait: Seconds a unit has to come back under a new invocation.
        started: Filled with ``{unit: {"invocation", "since"}}`` for each unit
            that started anew, for :func:`smoke`.

    Returns:
        Units that were active before but did not come back anew.
    """
    failed: list[str] = []
    for unit in units:
        was = _run(["systemctl", "--user", "is-active", unit], runner, timeout=30).stdout.strip()
        before = _unit_show(unit, runner).get("InvocationID", "") if was == "active" else ""
        try:
            _run(["systemctl", "--user", "try-restart", unit], runner, timeout=120)
        except subprocess.TimeoutExpired:
            pass  # a long stop is not a failure; the new-start wait below decides
        if was != "active":
            continue
        if not before:
            time.sleep(1)
            now = _run(
                ["systemctl", "--user", "is-active", unit], runner, timeout=30
            ).stdout.strip()
            if now != "active":
                failed.append(f"{unit} is {now} after restart")
            continue
        deadline = time.monotonic() + wait
        show: dict[str, str] = {}
        while True:
            show = _unit_show(unit, runner)
            inv = show.get("InvocationID", "")
            if show.get("ActiveState") == "active" and inv and inv != before:
                break
            if time.monotonic() >= deadline:
                same = "the same invocation" if inv == before else f"invocation {inv or 'none'}"
                failed.append(
                    f"{unit} did not start anew within {wait}s "
                    f"(state {show.get('ActiveState') or 'unknown'}, {same})"
                )
                show = {}
                break
            time.sleep(2)
        if not show:
            continue
        time.sleep(1)
        now = _run(["systemctl", "--user", "is-active", unit], runner, timeout=30).stdout.strip()
        if now != "active":
            failed.append(f"{unit} is {now} after restart")
        elif started is not None:
            started[unit] = {
                "invocation": show["InvocationID"],
                "since": show.get("ActiveEnterTimestamp", ""),
            }
    return failed


def smoke(
    manifest: Manifest,
    units: list[str],
    venv: Path,
    runner: Runner = subprocess.run,
    node: Optional[str] = None,
    started: Optional[dict[str, dict[str, str]]] = None,
) -> list[str]:
    """Run the global smokes and the restarted units' smokes from *venv*.

    A unit smoke runs with ``SK_DEPLOY_UNIT``, and, when *started* knows the
    unit's new start, ``SK_DEPLOY_INVOCATION_ID`` and ``SK_DEPLOY_SINCE`` (its
    ActiveEnterTimestamp), so it can judge that start only: for example
    ``journalctl --user _SYSTEMD_INVOCATION_ID=$SK_DEPLOY_INVOCATION_ID`` or
    ``journalctl --since "$SK_DEPLOY_SINCE"``.
    """
    env = dict(os.environ, PATH=f"{venv / 'bin'}:{os.environ.get('PATH', '')}")
    services = manifest.services_for(node)
    jobs: list[tuple[str, dict]] = [(cmd, env) for cmd in manifest.smoke]
    for u in units:
        if u in services and services[u].smoke:
            unit_env = dict(env, SK_DEPLOY_UNIT=u)
            info = (started or {}).get(u)
            if info:
                unit_env["SK_DEPLOY_INVOCATION_ID"] = info["invocation"]
                unit_env["SK_DEPLOY_SINCE"] = info["since"]
            jobs.append((services[u].smoke, unit_env))
    bad = []
    for cmd, cmd_env in jobs:
        proc = _run(["bash", "-c", cmd], runner, timeout=180, env=cmd_env)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-300:]
            bad.append(f"smoke `{cmd}` exit {proc.returncode}: {tail}")
    return bad


# -------------------------------------------------------------------------- apply


@dataclass
class Result:
    """Outcome of an apply or rollback."""

    ok: bool
    release: Optional[Path] = None
    previous: Optional[Path] = None
    restarted: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    rolled_back: bool = False
    #: things worth knowing that are not failures (a rollback over hand edits)
    notes: list[str] = field(default_factory=list)


def apply(
    manifest: Manifest,
    manifest_id: str,
    paths: Paths,
    python: str,
    runner: Runner = subprocess.run,
    node: Optional[str] = None,
    restart: bool = True,
    allow_missing_bin: tuple[str, ...] = (),
    log: Callable[[str], None] = lambda _m: None,
) -> Result:
    """Roll this node onto *manifest*. See the module docstring for the order.

    Args:
        manifest: The release set.
        manifest_id: The manifest commit (names the release directory).
        paths: The node's deploy layout.
        python: Interpreter to build the venv with.
        runner: subprocess.run, replaceable in tests.
        node: Node profile (this host by default).
        restart: Restart affected units (off for a build-and-switch only).
        allow_missing_bin: bin/ names the new venv may drop on purpose.
        log: Progress callback.

    Returns:
        The result. ``ok`` False with ``rolled_back`` True means production
        was switched, failed its smoke, and was switched back.
    """
    live = paths.venv_link.resolve() if paths.venv_link.exists() else None
    plan = make_plan(manifest, paths.venv_link / "bin" / "python", node) if live else Plan()
    log("pre-flight: checking every release tag is on its default branch")
    preflight(manifest, runner, node)
    dest = paths.releases / f"{time.strftime('%Y%m%d-%H%M%S')}-{manifest_id[:12]}"
    log(f"build: {dest}")
    build_venv(manifest, dest, python, runner, node)
    carried = carry_bin(manifest, live, dest, node)
    if carried:
        log(f"carried bin/: {', '.join(carried)}")
    log("verify: imports, entry points, pip check, bin/")
    problems = verify_venv(manifest, dest, live, runner, node, allow_missing_bin)
    if problems:
        return Result(ok=False, release=dest, problems=problems)
    tools = ToolsChange()
    unit_dir = default_unit_dir(Path.home())
    if manifest.tools_for(node):
        log("tools: export from main, publish, rewrite units")
        try:
            tools = apply_tools(
                manifest, node, paths.state_dir, paths.tools_root, unit_dir, runner=runner, log=log
            )
        except ToolError as exc:
            return Result(ok=False, release=dest, problems=[str(exc)])
    log(f"switch: {paths.venv_link} -> {dest}")
    previous = switch(paths, dest)
    state = load_state(paths)
    state.update(
        {
            "host": socket.gethostname(),
            "current": {"release": str(dest), "manifest": manifest_id, "at": time.time()},
            "previous": {"release": str(previous)} if previous else state.get("current"),
        }
    )
    state.setdefault("history", []).append(
        {"release": str(dest), "manifest": manifest_id, "at": time.time()}
    )
    save_state(paths, state)
    units = plan.restarts if live else list(manifest.services_for(node))
    units += [u for u in restart_targets(tools.units, runner) if u not in units]
    if not restart:
        return Result(
            ok=True,
            release=dest,
            previous=previous,
            notes=_record(paths, tools, manifest_id, unit_dir, log),
        )
    log(f"restart: {', '.join(units) or 'nothing'}")
    started: dict[str, dict[str, str]] = {}
    problems = restart_units(units, runner, started=started)
    problems += smoke(manifest, units, paths.venv_link, runner, node, started=started)
    if not problems:
        return Result(
            ok=True,
            release=dest,
            previous=previous,
            restarted=units,
            notes=_record(paths, tools, manifest_id, unit_dir, log),
        )
    log("smoke failed: switching back")
    if not tools.empty:
        tools.undo(paths.tools_root or default_root(), unit_dir)
        _run(["systemctl", "--user", "daemon-reload"], runner, timeout=60)
    if previous is not None:
        switch(paths, previous)
        restart_units(units, runner)
        state["current"], state["previous"] = {
            "release": str(previous),
            "rollback_of": str(dest),
            "at": time.time(),
        }, None
        save_state(paths, state)
    return Result(
        ok=False,
        release=dest,
        previous=previous,
        restarted=units,
        problems=problems,
        rolled_back=True,
    )


def apply_tools_only(
    manifest: Manifest,
    paths: Paths,
    runner: Runner = subprocess.run,
    node: Optional[str] = None,
    restart: bool = True,
    log: Callable[[str], None] = lambda _m: None,
    manifest_id: str = "",
) -> Result:
    """Roll only the tool exports (no venv rebuild), undoing them on failure.

    A change that sticks is recorded (:func:`save_change`) for
    :func:`rollback_tools`.
    """
    unit_dir = default_unit_dir(Path.home())
    try:
        tools = apply_tools(
            manifest, node, paths.state_dir, paths.tools_root, unit_dir, runner=runner, log=log
        )
    except ToolError as exc:
        return Result(ok=False, release=None, problems=[str(exc)])
    if tools.empty:
        return Result(ok=True, release=None)
    if not restart:
        return Result(
            ok=True, release=None, notes=_record(paths, tools, manifest_id, unit_dir, log)
        )
    units = restart_targets(tools.units, runner)
    log(f"restart: {', '.join(units) or 'nothing'}")
    started: dict[str, dict[str, str]] = {}
    problems = restart_units(units, runner, started=started)
    if not problems:
        problems = smoke(manifest, units, paths.venv_link, runner, node, started=started)
    if not problems:
        return Result(
            ok=True,
            release=None,
            restarted=units,
            notes=_record(paths, tools, manifest_id, unit_dir, log),
        )
    log("restart or smoke failed: restoring the previous tools")
    tools.undo(paths.tools_root or default_root(), unit_dir)
    _run(["systemctl", "--user", "daemon-reload"], runner, timeout=60)
    restart_units(units, runner)
    return Result(ok=False, release=None, restarted=units, problems=problems, rolled_back=True)


def rollback(paths: Paths, units: list[str], runner: Runner = subprocess.run) -> Result:
    """Switch back to the previous release and restart *units*."""
    state = load_state(paths)
    prev = (state.get("previous") or {}).get("release")
    if not prev or not Path(prev).is_dir():
        raise DeployError("no previous release recorded on this node")
    current = switch(paths, Path(prev))
    state["current"], state["previous"] = {"release": prev, "at": time.time()}, {
        "release": str(current)
    }
    save_state(paths, state)
    problems = restart_units(units, runner)
    return Result(
        ok=not problems, release=Path(prev), previous=current, restarted=units, problems=problems
    )


# ------------------------------------------------------------ tools change record

#: Under ``state_dir``: one record per tools change that stuck, plus ``last.json``.
CHANGES_DIR = "tools-changes"
LAST = "last.json"


def _changes(paths: Paths) -> Path:
    return paths.state_dir / CHANGES_DIR


def _write_record(path: Path, record: dict) -> None:
    """Write *record* atomically, mode 0600: previous file texts may hold secrets."""
    data = json.dumps(record, indent=1, sort_keys=True).encode("utf-8")
    _atomic_write(path, data, 0o600)


def save_change(
    paths: Paths, change: ToolsChange, manifest_id: str, unit_dir: Optional[Path] = None
) -> Path:
    """Record *change* as ``state_dir/tools-changes/<epoch>.json`` and point ``last.json`` at it.

    The record is plain JSON (mode 0600, its directory 0700): the previous
    ``current`` targets, drop-ins, unit files, moved drop-ins, managed files and
    legacy drop-ins to restore, the units to restart, the manifest commit, and
    the sha256 of what this change wrote (``written``), so a later process can
    undo it and tell what was edited since.

    Returns:
        The record's path.
    """
    d = _changes(paths)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    now = time.time()
    stem, n = str(int(now)), 0
    path = d / f"{stem}.json"
    while path.exists():
        n += 1
        path = d / f"{stem}-{n}.json"
    prev = last_change_path(paths)
    record = {
        **change.to_dict(),
        "at": now,
        "manifest": manifest_id,
        "tools_root": str(paths.tools_root or default_root()),
        "unit_dir": str(unit_dir or default_unit_dir(Path.home())),
        "previous": prev.name if prev else None,
        "rolled_back": False,
    }
    _write_record(path, record)
    _point_last(paths, path.name)
    return path


def _point_last(paths: Paths, name: Optional[str]) -> None:
    last = _changes(paths) / LAST
    if name is None:
        last.unlink(missing_ok=True)
        return
    tmp = last.with_name(f".{LAST}.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    os.symlink(name, tmp)
    os.replace(tmp, last)


def last_change_path(paths: Paths) -> Optional[Path]:
    """The record ``last.json`` points at, or None."""
    last = _changes(paths) / LAST
    if not last.is_symlink():
        return None
    path = _changes(paths) / os.readlink(last)
    return path if path.is_file() else None


def last_change(paths: Paths) -> Optional[dict]:
    """The last tools change that was not rolled back, or None."""
    path = last_change_path(paths)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record(paths: Paths, change: ToolsChange, manifest_id: str, unit_dir: Path, log) -> list[str]:
    """Save *change* if it changed anything; a failed save is a note, not a failure."""
    if change.empty:
        return []
    try:
        log(f"tools change recorded: {save_change(paths, change, manifest_id, unit_dir)}")
        return []
    except OSError as exc:
        note = f"the tools change was not recorded ({exc}); deploy rollback cannot undo it"
        log(note)
        return [note]


def _disk_state(path: Path, mode: str):
    if mode == "link":
        return os.readlink(path) if path.is_symlink() else None
    if path.is_symlink() and not path.exists():
        return None
    return _sha_bytes(path.read_bytes()) if path.is_file() else None


def _restore_targets(change: ToolsChange, unit_dir: Path) -> dict[str, Callable[[], bool]]:
    """Each path the rollback overwrites, with a check that it already is restored."""
    out: dict[str, Callable[[], bool]] = {}
    for key, was in change.files.entries():
        out[key] = lambda p=Path(key), w=was: _files.restored(p, w)
    for unit, old in change.unit_files.items():
        out[str(unit_dir / unit)] = lambda p=unit_dir / unit, o=old: _text_is(p, o)
    for unit, old in change.dropins.items():
        path = unit_dir / f"{unit}.d" / DROPIN
        out.setdefault(str(path), lambda p=path, o=old: _text_is(p, o))
    return out


def _text_is(path: Path, text: Optional[str]) -> bool:
    if text is None:
        return not (path.exists() or path.is_symlink())
    return path.is_file() and path.read_text(encoding="utf-8") == text


def _edited(change: ToolsChange, unit_dir: Path) -> list[str]:
    """Paths this rollback overwrites whose disk state is not what the change wrote.

    A path already back at its previous state (a rollback that stopped partway
    and runs again) is not an edit.
    """
    out: list[str] = []
    for key, done in _restore_targets(change, unit_dir).items():
        wrote = change.written.get(key)
        if wrote is None or done():
            continue
        mode = wrote.get("mode", "")
        want = wrote.get("target") if mode == "link" else wrote.get("sha256")
        if _disk_state(Path(key), mode) != want:
            out.append(key)
    out += [
        orig
        for orig, aside in change.displaced.items()
        if Path(orig).exists() and Path(aside).exists()
    ]
    return sorted(set(out))


def _blockers(change: ToolsChange, root: Path, unit_dir: Path) -> list[str]:
    """What stops *change* from being undone cleanly; checked before anything changes."""
    out = [
        f"{name}/{target} (the export {name}/current went back to) is gone"
        for name, target in change.published.items()
        if target is not None and not (root / name / target).is_dir()
    ]
    for original, moved in change.displaced.items():
        problem = displaced_blocker(original, moved)
        if problem and problem != "restored":
            out.append(problem)
    for key, was in change.files.entries():
        path = Path(key)
        if _files.restored(path, was):
            continue
        if was[0] == "moved":
            problem = _files.moved_back_blocker(path, was[1])
            if problem:
                out.append(problem)
        elif was[0] in ("file", "link") and path.is_dir() and not path.is_symlink():
            out.append(f"{key} is a real directory now; restoring its {was[0]} would fail")
    for unit, old in change.unit_files.items():
        if old is not None and (unit_dir / unit).is_dir():
            out.append(f"{unit_dir / unit} is a directory now")
    return out


def _open_record(paths: Paths, path: Path, record: dict):
    try:
        change = ToolsChange.from_dict(record)
    except (KeyError, TypeError, ValueError) as exc:
        raise DeployError(f"the tools change record {path} is unreadable: {exc}") from exc
    root = Path(record.get("tools_root") or paths.tools_root or default_root())
    unit_dir = Path(record.get("unit_dir") or default_unit_dir(Path.home()))
    return change, root, unit_dir


def pending_tools_changes(paths: Paths, since: float = 0.0) -> list[tuple[Path, dict]]:
    """The records a rollback would undo, newest first: every live one newer than *since*."""
    out: list[tuple[Path, dict]] = []
    path = last_change_path(paths)
    seen: set[str] = set()
    while path is not None and path.name not in seen:
        seen.add(path.name)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            break
        if record.get("rolled_back") or record.get("at", 0) <= since:
            break
        out.append((path, record))
        name = _previous_live(paths, record.get("previous"))
        path = _changes(paths) / name if name else None
    return out


def check_tools_rollback(paths: Paths, records: list[tuple[Path, dict]]) -> None:
    """Refuse, changing nothing, when any of *records* could not be undone.

    Each record is checked against the disk as it is now, so a later record's
    check is the best estimate before the newer ones are undone. Two records
    that both moved something aside at the same path are refused outright:
    that is the case where today's disk says nothing about the older one.

    Raises:
        DeployError: The problems, by record.
    """
    problems: list[str] = []
    moved_by: dict[str, str] = {}
    for path, record in records:
        change, root, unit_dir = _open_record(paths, path, record)
        problems += [f"{path.name}: {p}" for p in _blockers(change, root, unit_dir)]
        moved = [k for k, was in change.files.entries() if was[0] == "moved"]
        for key in dict.fromkeys(moved + list(change.displaced)):
            if key in moved_by:
                problems.append(
                    f"{key} was moved aside by both {moved_by[key]} and {path.name}; "
                    "undoing both would put one directory back over the other. "
                    "Restore it by hand from the state directory"
                )
            moved_by.setdefault(key, path.name)
    if problems:
        raise DeployError("cannot roll back the tools change: " + "; ".join(problems))


def rollback_tools(paths: Paths, runner: Runner = subprocess.run) -> Result:
    """Undo the last recorded tools change, reload systemd and restart its units.

    The record is marked ``rolled_back`` and ``last.json`` moves to the newest
    earlier record that was not, so a second rollback steps back one more change.
    Every undo step is idempotent: a rollback that stopped partway leaves its
    record unmarked, and running it again finishes the job.

    Returns:
        The result; ``notes`` lists the paths edited since that apply (their
        text was overwritten with the recorded one all the same).

    Raises:
        DeployError: No change is recorded, or something it would restore is
            gone or in the way (nothing was changed); or the undo stopped
            partway (run it again).
    """
    pending = pending_tools_changes(paths)
    if not pending:
        raise DeployError(
            "no recorded tools change on this node (tools applied before change records "
            "existed cannot be rolled back; deploy an earlier manifest with --ref instead)"
        )
    path, record = pending[0]
    check_tools_rollback(paths, [pending[0]])
    change, root, unit_dir = _open_record(paths, path, record)
    notes = _edited(change, unit_dir)
    try:
        change.undo(root, unit_dir)
    except (OSError, ToolError) as exc:
        raise DeployError(
            f"the rollback of {path.name} stopped partway ({exc}); its record is kept,"
            " run deploy rollback again to finish it"
        ) from exc
    record.update(rolled_back=True, rolled_back_at=time.time())
    _write_record(path, record)
    _point_last(paths, _previous_live(paths, record.get("previous")))
    _run(["systemctl", "--user", "daemon-reload"], runner, timeout=60)
    units = restart_targets(change.units, runner)
    problems = restart_units(units, runner)
    return Result(ok=not problems, restarted=units, problems=problems, notes=notes)


def _previous_live(paths: Paths, name: Optional[str]) -> Optional[str]:
    """Follow the ``previous`` chain from *name* to the first record not rolled back."""
    seen: set[str] = set()
    while name and name not in seen:
        seen.add(name)
        try:
            rec = json.loads((_changes(paths) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not rec.get("rolled_back"):
            return name
        name = rec.get("previous")
    return None
