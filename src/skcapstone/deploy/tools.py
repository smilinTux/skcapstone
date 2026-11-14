"""Tool exports: released copies of the scripts and small servers units run.

Plenty of user units execute a script straight out of a repository checkout
(``~/clawd/skcapstone-repos/skchat/scripts/nostr_relay.py``). That makes
production whatever branch the shared checkout is on, and when the checkout
loses the file the unit breaks on its next restart. A *tool* in the manifest
fixes both:

1. ``export``: a bare mirror of the repository is fetched, the tool's ref is
   resolved and checked to be on the default branch, and ``git archive``
   writes it to ``~/.local/share/sk-tools/<name>/<sha12>`` (never a working
   tree, never edited in place).
2. ``publish``: ``<name>/current`` is switched atomically to that export.
3. ``rewrite``: every user unit that names a path under the tool's
   ``checkout`` gets one managed drop-in, ``zz-sk-deploy-tools.conf``, that
   re-states its ``ExecStart*``, ``WorkingDirectory`` and ``Environment``
   lines with the checkout replaced by ``current``. The unit files themselves
   are never edited, and deleting the drop-in reverts the unit.

The whole step is undoable: :class:`ToolsChange` remembers the previous
``current`` targets and drop-in contents, and :meth:`ToolsChange.undo`
restores them.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .inspect import _EXEC_KEYS, _unit_settings
from .manifest import Manifest

Runner = Callable[..., subprocess.CompletedProcess]

DROPIN = "zz-sk-deploy-tools.conf"
_MARK = "# Managed by `skcapstone deploy` (manifest tools). Regenerated on every\n"
_MARK += "# apply; edit the manifest, not this file. Delete it to revert the unit.\n"
_META = ".sk-tool.json"


class ToolError(RuntimeError):
    """A tool could not be exported; nothing was published."""


@dataclass
class ToolsChange:
    """What one tools step changed, and how to put it back."""

    exported: dict[str, str] = field(default_factory=dict)
    published: dict[str, Optional[str]] = field(default_factory=dict)
    dropins: dict[str, Optional[str]] = field(default_factory=dict)
    units: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.published and not self.dropins

    def undo(self, root: Path, unit_dir: Path) -> None:
        """Restore the previous ``current`` links and drop-in files."""
        for name, previous in self.published.items():
            link = root / name / "current"
            if previous is None:
                link.unlink(missing_ok=True)
            else:
                _atomic_link(link, previous)
        for unit, old in self.dropins.items():
            path = unit_dir / f"{unit}.d" / DROPIN
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(old, encoding="utf-8")


def default_root(home: Optional[Path] = None) -> Path:
    """Where tool exports live."""
    return (home or Path.home()) / ".local" / "share" / "sk-tools"


def _git(args: list[str], runner: Runner, cwd: Optional[Path] = None, **kw):
    return runner(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        timeout=kw.pop("timeout", 600),
        check=False,
        **kw,
    )


def _text(proc: subprocess.CompletedProcess) -> str:
    out = proc.stdout
    return (out.decode() if isinstance(out, bytes) else out or "").strip()


def _err(proc: subprocess.CompletedProcess) -> str:
    err = proc.stderr
    return (err.decode(errors="replace") if isinstance(err, bytes) else err or "").strip()[-300:]


def _atomic_link(link: Path, target: str) -> None:
    tmp = link.with_name(f".{link.name}.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def export(
    name: str,
    url: str,
    ref: str,
    branch: str,
    paths: list[str],
    root: Path,
    src_dir: Path,
    runner: Runner = subprocess.run,
) -> Path:
    """Export *ref* of *url* to ``root/name/<sha12>`` and return that directory.

    Raises:
        ToolError: The mirror could not be fetched, the ref does not resolve,
            or it is not on *branch*.
    """
    mirror = src_dir / f"{name}.git"
    if not mirror.is_dir():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        proc = _git(["clone", "--bare", "--quiet", url, str(mirror)], runner)
        if proc.returncode != 0:
            raise ToolError(f"{name}: clone {url} failed: {_err(proc)}")
    proc = _git(
        ["fetch", "--quiet", "--prune", "--tags", "origin", "+refs/heads/*:refs/heads/*"],
        runner,
        cwd=mirror,
    )
    if proc.returncode != 0:
        raise ToolError(f"{name}: fetch failed: {_err(proc)}")
    proc = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], runner, cwd=mirror)
    sha = _text(proc)
    if proc.returncode != 0 or not sha:
        raise ToolError(f"{name}: ref {ref} does not resolve in {url}")
    proc = _git(["merge-base", "--is-ancestor", sha, f"refs/heads/{branch}"], runner, cwd=mirror)
    if proc.returncode != 0:
        raise ToolError(f"{name}: {ref} ({sha[:12]}) is not on {branch}; tools ship from main")
    dest = root / name / sha[:12]
    if (dest / _META).is_file():
        return dest
    proc = _git(["archive", "--format=tar", sha, "--", *paths], runner, cwd=mirror)
    if proc.returncode != 0:
        raise ToolError(f"{name}: archive of {sha[:12]} failed: {_err(proc)}")
    staging = root / name / f".staging-{sha[:12]}-{os.getpid()}"
    staging.mkdir(parents=True, exist_ok=False)
    data = proc.stdout if isinstance(proc.stdout, bytes) else proc.stdout.encode()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(staging, filter="data")
    meta = {"name": name, "url": url, "ref": ref, "sha": sha, "paths": paths, "at": time.time()}
    (staging / _META).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.rename(staging, dest)
    return dest


def installed(root: Path, name: str) -> Optional[dict]:
    """The metadata of the published export of *name*, or None."""
    try:
        return json.loads((root / name / "current" / _META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _forms(path: str, home: Path) -> list[str]:
    """The spellings a unit may use for *path* (absolute, ``%h``, ``$HOME``)."""
    out = [path]
    if path.startswith(str(home) + "/") or path == str(home):
        rest = path[len(str(home)) :]
        out += [f"%h{rest}", f"${{HOME}}{rest}", f"$HOME{rest}"]
    return out


def rewrite_pairs(manifest: Manifest, node: Optional[str], root: Path, home: Path) -> list:
    """``(old spelling, new path)`` substitutions, longest first."""
    pairs: list[tuple[str, str]] = []
    for name, tool in manifest.tools_for(node).items():
        current = root / name / "current"
        new_base = (
            "%h" + str(current)[len(str(home)) :]
            if str(current).startswith(str(home) + "/")
            else str(current)
        )
        for checkout in tool.checkouts:
            checkout = str(Path(checkout).expanduser())
            for rel in tool.paths or [""]:
                old = f"{checkout}/{rel}".rstrip("/")
                new = f"{new_base}/{rel}".rstrip("/")
                pairs += [(form, new) for form in _forms(old, home)]
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def _substitute(value: str, pairs: list[tuple[str, str]]) -> str:
    for old, new in pairs:
        value = re.sub(re.escape(old) + r"(?=$|[/\s\"':;=])", lambda _m, n=new: n, value)
    return value


def desired_dropins(unit_dir: Path, pairs: list[tuple[str, str]]) -> dict[str, str]:
    """The managed drop-in each unit needs, keyed by unit name."""
    out: dict[str, str] = {}
    if not pairs or not unit_dir.is_dir():
        return out
    for unit in sorted(unit_dir.glob("*.service")):
        settings = _unit_settings(unit, skip=(DROPIN,))
        lines: list[str] = []
        for key in _EXEC_KEYS:
            values = settings[key]
            new = [_substitute(v, pairs) for v in values]
            if new == values:
                continue
            if key == "WorkingDirectory":
                lines.append(f"WorkingDirectory={new[-1]}")
            else:
                lines += [f"{key}="] + [f"{key}={v}" for v in new]
        for value in settings["Environment"]:
            new = _substitute(value, pairs)
            if new != value:
                lines.append(f"Environment={new}")
        if lines:
            out[unit.name] = _MARK + "[Service]\n" + "\n".join(lines) + "\n"
    return out


def _missing_in_exports(
    want: dict[str, str], exports: dict[str, Path], root: Path, home: Path
) -> list[str]:
    """Paths the new drop-ins name under a tool's ``current`` that its export lacks.

    Checked against the NEW export before anything is published, so a ref on
    main that dropped a script a unit runs is refused instead of breaking the
    unit on its next start.
    """
    gone: list[str] = []
    for unit, text in want.items():
        for name, dest in exports.items():
            for base in (
                f"%h{str(root)[len(str(home)):]}/{name}/current",
                f"{root}/{name}/current",
            ):
                for match in re.finditer(re.escape(base) + r"(/[^\s\"':;]*)?", text):
                    rel = (match.group(1) or "").lstrip("/")
                    if not (dest / rel).exists():
                        gone.append(f"{unit}: {name}/{rel or '.'}")
    return sorted(set(gone))


def apply_tools(
    manifest: Manifest,
    node: Optional[str],
    state_dir: Path,
    root: Optional[Path] = None,
    unit_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    runner: Runner = subprocess.run,
    log: Callable[[str], None] = lambda _m: None,
) -> ToolsChange:
    """Export, publish and rewrite every tool for *node*.

    All exports happen before anything is published, so a tool that cannot
    be exported leaves the node exactly as it was.

    Returns:
        The change, with the units whose effective command changed (a new
        drop-in, or a tool whose ``current`` moved) in ``units``.

    Raises:
        ToolError: An export failed; nothing was published.
    """
    home = home or Path.home()
    root = root or default_root(home)
    from .inspect import default_unit_dir

    unit_dir = unit_dir or default_unit_dir(home)
    change = ToolsChange()
    exports: dict[str, Path] = {}
    for name, tool in manifest.tools_for(node).items():
        url, ref, branch = manifest.tool_source(name)
        exports[name] = export(
            name, url, ref, branch, tool.paths, root, state_dir / "tool-src", runner
        )
        change.exported[name] = exports[name].name
    pairs = rewrite_pairs(manifest, node, root, home)
    want = desired_dropins(unit_dir, pairs)
    gone = _missing_in_exports(want, exports, root, home)
    if gone:
        raise ToolError(
            "units would point at files the export does not have: " + "; ".join(gone[:8])
        )
    moved: set[str] = set()
    for name, dest in exports.items():
        link = root / name / "current"
        previous = os.readlink(link) if link.is_symlink() else None
        if previous != dest.name:
            change.published[name] = previous
            _atomic_link(link, dest.name)
            moved.add(name)
            log(f"tool {name}: {previous or 'none'} -> {dest.name}")
    have = {p.parent.name.removesuffix(".d"): p for p in unit_dir.glob(f"*.service.d/{DROPIN}")}
    for unit in sorted(set(want) | set(have)):
        path = unit_dir / f"{unit}.d" / DROPIN
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        new = want.get(unit)
        if old == new:
            continue
        change.dropins[unit] = old
        if new is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new, encoding="utf-8")
    affected = set(change.dropins)
    for unit, text in want.items():
        if any(f"/sk-tools/{name}/current" in text for name in moved):
            affected.add(unit)
    change.units = sorted(affected)
    if change.dropins:
        runner(
            ["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=60, check=False
        )
    return change


def restart_targets(units: list[str], runner: Runner = subprocess.run) -> list[str]:
    """Concrete units to try-restart: templates expand to their live instances."""
    out: list[str] = []
    for unit in units:
        if not unit.endswith("@.service"):
            out.append(unit)
            continue
        proc = runner(
            [
                "systemctl",
                "--user",
                "list-units",
                "--plain",
                "--no-legend",
                "--state=active",
                unit.replace("@.service", "@*.service"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        out += [line.split()[0] for line in (proc.stdout or "").splitlines() if line.split()]
    return out
