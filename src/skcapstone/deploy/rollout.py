"""Roll a node onto a release manifest, and back off it.

The order is fixed and every step can stop the rollout before production
changes:

1. pre-flight: every pinned release is a tag on its repository's default
   branch (a version that is not on main is refused, not warned about);
2. build a NEW venv next to the live one, non-editable, from the manifest;
3. verify that venv in place (imports, console-script targets, ``pip check``,
   no executable lost from ``bin/`` compared with the live venv);
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

from .inspect import list_dists, normalize
from .manifest import Manifest

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

    @classmethod
    def default(cls, home: Optional[Path] = None) -> "Paths":
        """The standard layout under *home*."""
        home = home or Path.home()
        return cls(
            venv_link=home / ".skenv",
            releases=home / ".skenv-releases",
            state_dir=home / ".local" / "state" / "sk-deploy",
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
    installed = {normalize(d.name): d for d in list_dists(live_python)}
    plan = Plan()
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
    try:
        proc = _run(["uv", "venv", "--seed", "--python", python, str(dest)], runner)
        if proc.returncode != 0:
            raise DeployError(f"uv venv failed: {proc.stderr.strip()[-500:]}")
        proc = _run(
            ["uv", "pip", "install", "--python", str(dest / "bin" / "python"), "-r", str(reqs)],
            runner,
            timeout=3600,
        )
        if proc.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise DeployError(f"uv pip install failed: {proc.stderr.strip()[-1500:]}")
    finally:
        reqs.unlink(missing_ok=True)


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
    proc = _run(["uv", "pip", "check", "--python", str(dest / "bin" / "python")], runner)
    if proc.returncode != 0:
        problems.append("pip check: " + (proc.stdout + proc.stderr).strip()[-800:])
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
            and not p.name.startswith(("activate", "python", "pip"))
        )
        if lost:
            problems.append(f"bin/ would lose {len(lost)} executable(s): {', '.join(lost)}")
    return problems


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


def restart_units(units: list[str], runner: Runner = subprocess.run) -> list[str]:
    """``try-restart`` each unit (only running units restart) and confirm it.

    Returns:
        Units that were active before but are not active afterwards.
    """
    failed: list[str] = []
    for unit in units:
        was = _run(["systemctl", "--user", "is-active", unit], runner, timeout=30).stdout.strip()
        _run(["systemctl", "--user", "try-restart", unit], runner, timeout=120)
        if was == "active":
            time.sleep(1)
            now = _run(
                ["systemctl", "--user", "is-active", unit], runner, timeout=30
            ).stdout.strip()
            if now != "active":
                failed.append(f"{unit} is {now} after restart")
    return failed


def smoke(
    manifest: Manifest,
    units: list[str],
    venv: Path,
    runner: Runner = subprocess.run,
    node: Optional[str] = None,
) -> list[str]:
    """Run the global smokes and the restarted units' smokes from *venv*."""
    env = dict(os.environ, PATH=f"{venv / 'bin'}:{os.environ.get('PATH', '')}")
    cmds = list(manifest.smoke)
    services = manifest.services_for(node)
    cmds += [services[u].smoke for u in units if u in services and services[u].smoke]
    bad = []
    for cmd in cmds:
        proc = _run(["bash", "-c", cmd], runner, timeout=180, env=env)
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
    log("verify: imports, entry points, pip check, bin/")
    problems = verify_venv(manifest, dest, live, runner, node, allow_missing_bin)
    if problems:
        return Result(ok=False, release=dest, problems=problems)
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
    if not restart:
        return Result(ok=True, release=dest, previous=previous)
    log(f"restart: {', '.join(units) or 'nothing'}")
    problems = restart_units(units, runner)
    problems += smoke(manifest, units, paths.venv_link, runner, node)
    if not problems:
        return Result(ok=True, release=dest, previous=previous, restarted=units)
    log("smoke failed: switching back")
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
