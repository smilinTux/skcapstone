"""Read-only views of the stacks and tools step: ``deploy plan`` and ``deploy status``.

Nothing here publishes an export or writes to the node. The one scratch
directory ``plan`` needs (the config instance at its ref, to render managed
files against) is a temporary directory that is removed before it returns.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import files as _files
from .inspect import default_unit_dir
from .manifest import Manifest
from .tools import (
    _META,
    _UNIT_SUFFIXES,
    CONFIG_SUFFIX,
    DROPIN,
    LEGACY_DROPINS,
    STACK_RELEASE_SUFFIX,
    ToolError,
    _git,
    default_root,
    desired_dropins,
    file_restarts,
    installed,
    ref_on_branch,
    render_units,
    restart_units_for,
    rewrite_pairs,
    sync_mirror,
)


def _unpack(cname, cfg, mirror: Path, runner) -> tuple[Path, str, str]:
    """The config instance at its ref, unpacked to a temp dir (its commit and the dir)."""
    url, ref, branch = cfg.source()
    sync_mirror(cname, url, mirror, runner)
    sha = ref_on_branch(cname, url, ref, branch, mirror, runner)
    proc = _git(["archive", "--format=tar", sha, "--", cfg.instance], runner, cwd=mirror)
    if proc.returncode != 0:
        raise ToolError(f"{cname}: archive of {sha[:12]} failed")
    tmp = Path(tempfile.mkdtemp(prefix="sk-deploy-plan-"))
    try:
        data = proc.stdout if isinstance(proc.stdout, bytes) else proc.stdout.encode()
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter="data")
        # what render_units reads of an export, so the rendered text is apply's
        (tmp / _META).write_text(json.dumps({"url": url, "sha": sha}), encoding="utf-8")
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, sha, ref


def _plan_units(manifest, node, root, home, unit_dir, exports, moved, file_units) -> list[str]:
    """The units apply would restart, computed from the same rules without publishing."""
    rendered = render_units(manifest, node, root, home, exports)
    pairs = rewrite_pairs(manifest, node, root, home)
    want = {u: t for u, t in desired_dropins(unit_dir, pairs).items() if u not in rendered}
    changed: set[str] = set(file_units)
    displaced: list[str] = []
    for legacy in LEGACY_DROPINS:
        for path in unit_dir.glob(f"*.service.d/{legacy}"):
            changed.add(path.parent.name.removesuffix(".d"))
    for unit, text in rendered.items():
        dropdir = unit_dir / f"{unit}.d"
        displaced += [str(c) for c in sorted(dropdir.glob("*.conf"))] if dropdir.is_dir() else []
        path = unit_dir / unit
        if (path.read_text(encoding="utf-8") if path.is_file() else None) != text:
            changed.add(unit)
    have = {p.parent.name.removesuffix(".d"): p for p in unit_dir.glob(f"*.service.d/{DROPIN}")}
    for unit in set(want) | set(have):
        path = unit_dir / f"{unit}.d" / DROPIN
        if (path.read_text(encoding="utf-8") if path.is_file() else None) != want.get(unit):
            changed.add(unit)
    return restart_units_for(
        manifest,
        node,
        changed=changed,
        displaced=displaced,
        texts={**want, **rendered},
        moved=moved,
    )


def tools_plan(
    manifest: Manifest,
    node: Optional[str],
    state_dir: Path,
    package_restarts: list[str],
    home: Optional[Path] = None,
    root: Optional[Path] = None,
    runner=subprocess.run,
    unit_dir: Optional[Path] = None,
) -> dict:
    """What the stacks and tools step would show for *node*, changing nothing on it.

    Returns:
        ``stacks`` (name to ref and commit), ``roles``, ``tools`` (each tool
        export that would move, from its installed ref to the manifest's),
        ``files`` (per config export: its ref and the managed file diffs, or
        an ``error``), ``restarts`` (the units apply restarts, in its order;
        the package restarts come first), ``restarts_complete`` (False when
        an error kept some of them from being computed), ``errors`` (every
        config or restart computation that failed) and ``notes`` (a managed
        file that changes with no unit to restart for it).
    """
    home = home or Path.home()
    root = root or default_root(home)
    unit_dir = unit_dir or default_unit_dir(home)
    prof = manifest.profile(node)
    stacks = {}
    for name, sref in manifest.stacks.items():
        release = name + STACK_RELEASE_SUFFIX
        sha = manifest.tool_source(release)[1] if release in manifest.tools else None
        stacks[name] = {"ref": sref.ref, "sha": sha}
    moves: dict[str, dict] = {}
    for name in manifest.tools_for(node):
        want = manifest.tool_source(name)[1]
        meta = installed(root, name)
        have = meta.get("ref") if meta else None
        if have != want:
            moves[name] = {"from": have, "to": want}
    files: dict[str, dict] = {}
    exports: dict[str, Path] = {}
    file_units: set[str] = set()
    errors: list[str] = []
    notes: list[str] = []
    tmps: list[Path] = []
    try:
        for tool, cfg in manifest.configs_for(node).items():
            cname = tool + CONFIG_SUFFIX
            ref = cfg.source()[1]
            try:
                tmp, sha, _ = _unpack(cname, cfg, state_dir / "tool-src" / f"{cname}.git", runner)
                tmps.append(tmp)
                exports[cname] = tmp
                inst = tmp / cfg.instance
                specs = _files.load_files(inst)
                if not specs:
                    continue
                diffs = _files.diff_files(
                    specs, inst, home, tools_on_node=manifest.tools_for(node), tools_root=root
                )
                files[cname] = {"ref": ref, "sha": sha, "diffs": diffs}
                sysdir = inst / "systemd"
                inst_units = (
                    [f.name for f in sysdir.glob("*") if f.suffix in _UNIT_SUFFIXES]
                    if sysdir.is_dir()
                    else []
                )
                changed = _files.pending_changes(
                    specs, inst, home, tools_on_node=manifest.tools_for(node), tools_root=root
                )
                more, said = file_restarts(changed, inst_units)
                file_units |= more
                notes += said
            except (ToolError, OSError, ValueError, tarfile.TarError) as exc:
                files[cname] = {"ref": ref, "error": str(exc), "diffs": []}
                errors.append(f"files of {cname}: {exc}")
        units: list[str] = []
        if not errors:
            try:
                units = _plan_units(
                    manifest, node, root, home, unit_dir, exports, set(moves), file_units
                )
            except (ToolError, OSError, ValueError) as exc:
                errors.append(f"restarts: {exc}")
    finally:
        for tmp in tmps:
            shutil.rmtree(tmp, ignore_errors=True)
    restarts = list(dict.fromkeys([*package_restarts, *units]))
    return {
        "stacks": stacks,
        "roles": {k: list(v) for k, v in prof.roles.items()},
        "tools": moves,
        "files": files,
        "restarts": restarts,
        "restarts_complete": not errors,
        "errors": errors,
        "notes": notes,
    }


def last_tools_change(paths) -> Optional[dict]:
    """The last tools change that stuck: when, manifest, and per tool the sha now and before."""
    from .rollout import last_change

    rec = last_change(paths)
    if rec is None:
        return None
    exported = rec.get("exported") or {}
    published = rec.get("published") or {}
    tools = {
        name: {"now": now, "before": published.get(name, now)} for name, now in exported.items()
    }
    return {
        "at": rec.get("at"),
        "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.get("at", 0))),
        "manifest": rec.get("manifest"),
        "tools": tools,
        "units": rec.get("units") or [],
    }
