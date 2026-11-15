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
   ``checkout`` gets one managed drop-in, ``zzzz-sk-deploy-tools.conf``, that
   re-states its ``Exec*`` (start, stop, reload), ``WorkingDirectory`` and ``Environment``
   lines with the checkout replaced by ``current``. The unit files themselves
   are never edited, and deleting the drop-in reverts the unit.

A node can also pin a per-cluster **config repository** for a tool
(``nodes.<node>.configs.<tool>`` in the manifest). It is exported the same
way, as ``<tool>-config``, holding only that node's ``<instance>/`` directory,
and every unit file in ``<instance>/systemd/`` is rendered (``@TOOL@`` and
``@CONFIG@`` replaced by the two ``current`` exports) and installed as the
whole unit: hand-written drop-ins of a rendered unit are moved aside under
the deploy state directory. So the framework release, the cluster config and
the unit that joins them are all pinned, reviewed by PR, and nothing runs
from a working tree.

The whole step is undoable: :class:`ToolsChange` remembers the previous
``current`` targets, drop-in contents, unit files and moved drop-ins, and
:meth:`ToolsChange.undo` restores them.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .inspect import _EXEC_KEYS, _unit_settings
from .manifest import Manifest

Runner = Callable[..., subprocess.CompletedProcess]

#: systemd applies drop-ins in name order and the last assignment wins, so the
#: managed one must sort after every hand-written drop-in (``zz-*`` included).
DROPIN = "zzzz-sk-deploy-tools.conf"
#: Earlier names of the managed drop-in, removed on the next apply.
LEGACY_DROPINS = ("zz-sk-deploy-tools.conf",)
_MARK = "# Managed by `skcapstone deploy` (manifest tools). Regenerated on every\n"
_MARK += "# apply; edit the manifest, not this file. Delete it to revert the unit.\n"
_META = ".sk-tool.json"
#: Export name suffix of a tool's per-cluster config repository.
CONFIG_SUFFIX = "-config"
_PLACEHOLDER = re.compile(r"@[A-Z][A-Z0-9_]*@")
_UNIT_SUFFIXES = (".service", ".timer")


class ToolError(RuntimeError):
    """A tool could not be exported; nothing was published."""


@dataclass
class ToolsChange:
    """What one tools step changed, and how to put it back."""

    exported: dict[str, str] = field(default_factory=dict)
    published: dict[str, Optional[str]] = field(default_factory=dict)
    dropins: dict[str, Optional[str]] = field(default_factory=dict)
    units: list[str] = field(default_factory=list)
    legacy: dict[str, str] = field(default_factory=dict)
    #: rendered unit file name -> its previous content (None: it did not exist)
    unit_files: dict[str, Optional[str]] = field(default_factory=dict)
    #: drop-in moved aside from a rendered unit: original path -> where it went
    displaced: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.published or self.dropins or self.unit_files or self.displaced)

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
        for path, text in self.legacy.items():
            Path(path).write_text(text, encoding="utf-8")
        for unit, old in self.unit_files.items():
            path = unit_dir / unit
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(old, encoding="utf-8")
        for original, moved in self.displaced.items():
            Path(original).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(moved, original)


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


def sync_mirror(name: str, url: str, mirror: Path, runner: Runner = subprocess.run) -> None:
    """Clone *url* as a bare mirror at *mirror* if missing, then fetch it.

    Raises:
        ToolError: The clone or the fetch failed.
    """
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


def ref_on_branch(
    name: str,
    url: str,
    ref: str,
    branch: str,
    mirror: Path,
    runner: Runner = subprocess.run,
    what: str = "tools",
) -> str:
    """The commit *ref* names in *mirror*, checked to be on *branch*.

    Raises:
        ToolError: The ref does not resolve, or it is not on *branch*.
    """
    proc = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], runner, cwd=mirror)
    sha = _text(proc)
    if proc.returncode != 0 or not sha:
        raise ToolError(f"{name}: ref {ref} does not resolve in {url}")
    proc = _git(["merge-base", "--is-ancestor", sha, f"refs/heads/{branch}"], runner, cwd=mirror)
    if proc.returncode != 0:
        raise ToolError(f"{name}: {ref} ({sha[:12]}) is not on {branch}; {what} ship from main")
    return sha


def export(
    name: str,
    url: str,
    ref: str,
    branch: str,
    paths: list[str],
    root: Path,
    src_dir: Path,
    runner: Runner = subprocess.run,
    setup: Optional[list[str]] = None,
    setup_env: Optional[dict[str, str]] = None,
    setup_timeout: int = 1800,
) -> Path:
    """Export *ref* of *url* to ``root/name/<sha12>`` and return that directory.

    With *setup*, that argv runs inside the export at its final path (tools
    that generate a launcher or a dependency environment keyed to their own
    location), after extraction and before the export is marked complete. A
    failed setup removes the export, so the next apply starts clean.

    Raises:
        ToolError: The mirror could not be fetched, the ref does not resolve,
            or it is not on *branch*.
    """
    mirror = src_dir / f"{name}.git"
    sync_mirror(name, url, mirror, runner)
    sha = ref_on_branch(name, url, ref, branch, mirror, runner)
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
    if not setup:
        (staging / _META).write_text(json.dumps(meta, indent=1), encoding="utf-8")
        os.rename(staging, dest)
        return dest
    if dest.exists():  # an earlier setup that never finished: no metadata, start over
        shutil.rmtree(dest)
    os.rename(staging, dest)
    argv = [os.path.expanduser(a) for a in setup]
    env = {**os.environ, **{k: os.path.expanduser(v) for k, v in (setup_env or {}).items()}}
    try:
        proc = runner(
            argv, cwd=str(dest), env=env, capture_output=True, timeout=setup_timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise ToolError(f"{name}: setup could not run: {exc}") from exc
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise ToolError(f"{name}: setup failed ({proc.returncode}): {_err(proc)}")
    meta["setup"] = setup
    (dest / _META).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return dest


def _home_form(path: Path, home: Path) -> str:
    """*path* spelled with ``%h`` when it is under *home* (unit-file form)."""
    text = str(path)
    return "%h" + text[len(str(home)) :] if text.startswith(str(home) + "/") else text


_NAME = r"([A-Za-z0-9][A-Za-z0-9_.-]*)"
_TOOL_REF = re.compile(rf"@TOOL:{_NAME}@")
_STACK_REF = re.compile(rf"@STACK:{_NAME}@")
#: A stack's own repository is exported as this tool on nodes with its roles.
STACK_RELEASE_SUFFIX = "-release"


def _expand_named(
    pattern: re.Pattern, kind: str, text: str, known, target: Callable[[str], str], where: str
) -> str:
    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in known:
            raise ToolError(f"unknown {kind} {name}" + (f" in {where}" if where else ""))
        return target(name)

    return pattern.sub(_sub, text)


def expand_tool_refs(text: str, tools_on_node, home_form_root: str, where: str = "") -> str:
    """Replace ``@TOOL:<name>@`` with ``<home_form_root>/<name>/current``.

    Args:
        text: Unit file text (or a symlink target).
        tools_on_node: Names of the tools the node runs (any container of names).
        home_form_root: The tools root in unit-file form, e.g. ``%h/.local/share/sk-tools``.
        where: What the text belongs to, for the error message.

    Raises:
        ToolError: The name is not a tool the node runs.
    """
    return _expand_named(
        _TOOL_REF, "tool", text, tools_on_node, lambda n: f"{home_form_root}/{n}/current", where
    )


def expand_stack_refs(
    text: str, stacks, root: str, where: str = "", versions: Optional[dict[str, str]] = None
) -> str:
    """Replace ``@STACK:<name>@`` with the stack repo's export, ``<root>/<name>-release/current``.

    Args:
        text: A smoke command or a setup argument.
        stacks: Names of the stacks the manifest includes.
        root: The tools root as the consumer needs it spelled (an absolute
            path for smokes and setup argv, which no systemd ever expands).
        where: What the text belongs to, for the error message.
        versions: Stack name to its resolved commit: expand to the versioned
            export ``<name>-release/<sha12>`` instead of ``current``. Setup runs
            at export time, before any ``current`` is published, so it needs this.

    Raises:
        ToolError: The name is not a stack the manifest includes.
    """
    return _expand_named(
        _STACK_REF,
        "stack",
        text,
        stacks,
        lambda n: f"{root}/{n}{STACK_RELEASE_SUFFIX}/"
        + (versions[n][:12] if versions is not None else "current"),
        where,
    )


def render_units(
    manifest: Manifest, node: Optional[str], root: Path, home: Path, exports: dict[str, Path]
) -> dict[str, str]:
    """The unit files each node config renders, keyed by unit file name.

    Raises:
        ToolError: A config has no ``<instance>/systemd/`` units, a unit is
            rendered twice, or a placeholder is left unreplaced.
    """
    out: dict[str, str] = {}
    for tool, cfg in manifest.configs_for(node).items():
        cname = tool + CONFIG_SUFFIX
        dest = exports[cname]
        sysdir = dest / cfg.instance / "systemd"
        files = (
            sorted(f for f in sysdir.glob("*") if f.suffix in _UNIT_SUFFIXES)
            if sysdir.is_dir()
            else []
        )
        if not files:
            raise ToolError(f"{cname}: {cfg.instance}/systemd/ has no unit files")
        meta = json.loads((dest / _META).read_text(encoding="utf-8"))
        tool_cur = _home_form(root / tool / "current", home)
        cfg_cur = _home_form(root / cname / "current" / cfg.instance, home)
        node_tools = manifest.tools_for(node)
        tools_root = _home_form(root, home)
        for f in files:
            text = (
                expand_tool_refs(f.read_text(encoding="utf-8"), node_tools, tools_root, f.name)
                .replace("@TOOL@", tool_cur)
                .replace("@CONFIG@", cfg_cur)
            )
            left = sorted(set(_PLACEHOLDER.findall(text)))
            if left:
                raise ToolError(
                    f"{cname}: {cfg.instance}/systemd/{f.name} leaves {', '.join(left)}"
                )
            if f.name in out:
                raise ToolError(f"unit {f.name} is rendered by more than one config")
            head = (
                "# Managed by `skcapstone deploy`: rendered from "
                f"{cfg.instance}/systemd/{f.name}\n"
                f"# of {meta['url']} at {meta['sha'][:12]} ({cfg.ref}), with {tool} at its\n"
                "# manifest ref. Change it in the config repo by PR. Drop-ins for this\n"
                "# unit are moved aside on every apply: this file is the whole unit.\n"
            )
            out[f.name] = head + text
    return out


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
    """``(old spelling, new path)`` substitutions, longest first.

    Besides each checkout, a tool's own versioned exports (``<root>/<name>/<sha12>``)
    map to ``current``: a program that regenerates its unit from its resolved path
    (Hermes does at every boot) would otherwise leave no checkout path to rewrite,
    the next apply would drop the managed drop-in, and the unit would keep
    starting the old export after ``current`` moves. Those are regex pairs.
    """
    pairs: list[tuple] = []
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
        for form in _forms(str(root / name), home):
            pairs.append((re.compile(re.escape(form) + r"/[0-9a-f]{12}"), new_base))
    return sorted(pairs, key=lambda p: len(_src(p[0])), reverse=True)


def _src(old) -> str:
    return old.pattern if isinstance(old, re.Pattern) else old


def _substitute(value: str, pairs: list[tuple]) -> str:
    for old, new in pairs:
        pattern = old.pattern if isinstance(old, re.Pattern) else re.escape(old)
        value = re.sub(pattern + r"(?=$|[/\s\"':;=])", lambda _m, n=new: n, value)
    return value


def desired_dropins(unit_dir: Path, pairs: list[tuple[str, str]]) -> dict[str, str]:
    """The managed drop-in each unit needs, keyed by unit name."""
    out: dict[str, str] = {}
    if not pairs or not unit_dir.is_dir():
        return out
    for unit in sorted(unit_dir.glob("*.service")):
        settings = _unit_settings(unit, skip=(DROPIN, *LEGACY_DROPINS))
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
            name,
            url,
            ref,
            branch,
            tool.paths,
            root,
            state_dir / "tool-src",
            runner,
            setup=tool.setup,
            setup_env=tool.setup_env,
            setup_timeout=tool.setup_timeout,
        )
        change.exported[name] = exports[name].name
    for name, cfg in manifest.configs_for(node).items():
        cname = name + CONFIG_SUFFIX
        url, ref, branch = cfg.source()
        exports[cname] = export(
            cname, url, ref, branch, [cfg.instance], root, state_dir / "tool-src", runner
        )
        change.exported[cname] = exports[cname].name
    rendered = render_units(manifest, node, root, home, exports)
    pairs = rewrite_pairs(manifest, node, root, home)
    want = {u: t for u, t in desired_dropins(unit_dir, pairs).items() if u not in rendered}
    gone = _missing_in_exports({**want, **rendered}, exports, root, home)
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
    for legacy in LEGACY_DROPINS:
        for path in unit_dir.glob(f"*.service.d/{legacy}"):
            change.dropins.setdefault(path.parent.name.removesuffix(".d"), None)
            change.legacy[str(path)] = path.read_text(encoding="utf-8")
            path.unlink()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for unit, text in rendered.items():
        dropdir = unit_dir / f"{unit}.d"
        hand = sorted(dropdir.glob("*.conf")) if dropdir.is_dir() else []
        for conf in hand:
            aside = state_dir / "displaced-dropins" / stamp / f"{unit}.d" / conf.name
            aside.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(conf), str(aside))
            change.displaced[str(conf)] = str(aside)
            log(f"unit {unit}: drop-in {conf.name} moved aside to {aside}")
        if hand and dropdir.is_dir() and not any(dropdir.iterdir()):
            dropdir.rmdir()
        path = unit_dir / unit
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        if old != text:
            change.unit_files[unit] = old
            unit_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            log(f"unit {unit}: written from its config")
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
    affected = set(change.dropins) | set(change.unit_files)
    affected |= {Path(p).parent.name.removesuffix(".d") for p in change.displaced}
    for unit, text in {**want, **rendered}.items():
        if any(f"/sk-tools/{name}/current" in text for name in moved):
            affected.add(unit)
    change.units = sorted(affected)
    if change.dropins or change.unit_files or change.displaced:
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
