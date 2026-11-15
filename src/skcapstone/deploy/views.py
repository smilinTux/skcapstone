"""Read-only views of the stacks and tools step: ``deploy plan`` and ``deploy status``.

Nothing here publishes an export or writes to the node. The one scratch
directory ``plan`` needs (the config instance at its ref, to render managed
files against) is a temporary directory that is removed before it returns.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import files as _files
from .manifest import Manifest
from .tools import (
    _UNIT_SUFFIXES,
    CONFIG_SUFFIX,
    STACK_RELEASE_SUFFIX,
    ToolError,
    _git,
    default_root,
    installed,
    ref_on_branch,
    sync_mirror,
)


def _config_files(
    manifest: Manifest, node: Optional[str], state_dir: Path, root: Path, home: Path, runner
) -> tuple[dict[str, dict], list[str]]:
    """Managed file diffs per config, and the units those configs would restart.

    The config is read at its ref from the tool mirror (cloned or fetched
    like any apply would) and unpacked to a temporary directory.
    """
    out: dict[str, dict] = {}
    units: list[str] = []
    for tool, cfg in manifest.configs_for(node).items():
        cname = tool + CONFIG_SUFFIX
        url, ref, branch = cfg.source()
        tmp = Path(tempfile.mkdtemp(prefix="sk-deploy-plan-"))
        try:
            mirror = state_dir / "tool-src" / f"{cname}.git"
            sync_mirror(cname, url, mirror, runner)
            sha = ref_on_branch(cname, url, ref, branch, mirror, runner)
            proc = _git(["archive", "--format=tar", sha, "--", cfg.instance], runner, cwd=mirror)
            if proc.returncode != 0:
                raise ToolError(f"{cname}: archive of {sha[:12]} failed")
            data = proc.stdout if isinstance(proc.stdout, bytes) else proc.stdout.encode()
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                tar.extractall(tmp, filter="data")
            inst = tmp / cfg.instance
            specs = _files.load_files(inst)
            diffs = (
                _files.diff_files(
                    specs, inst, home, tools_on_node=manifest.tools_for(node), tools_root=root
                )
                if specs
                else []
            )
            meta = installed(root, cname)
            moves = meta is None or meta.get("ref") != ref
            sysdir = inst / "systemd"
            inst_units = (
                sorted(f.name for f in sysdir.glob("*") if f.suffix in _UNIT_SUFFIXES)
                if sysdir.is_dir()
                else []
            )
            if diffs or moves:
                units += inst_units
            if specs:
                out[cname] = {"ref": ref, "sha": sha, "diffs": diffs}
        except (ToolError, OSError, ValueError, tarfile.TarError) as exc:
            out[cname] = {"ref": ref, "error": str(exc), "diffs": []}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out, units


def tools_plan(
    manifest: Manifest,
    node: Optional[str],
    state_dir: Path,
    package_restarts: list[str],
    home: Optional[Path] = None,
    root: Optional[Path] = None,
    runner=subprocess.run,
) -> dict:
    """What the stacks and tools step would show for *node*, changing nothing on it.

    Returns:
        ``stacks`` (name to ref and commit), ``roles``, ``files`` (per config
        export: its ref and the managed file diffs, or an ``error``) and
        ``restarts`` (the package restarts, then the units of configs that change).
    """
    home = home or Path.home()
    root = root or default_root(home)
    prof = manifest.profile(node)
    stacks = {}
    for name, sref in manifest.stacks.items():
        release = name + STACK_RELEASE_SUFFIX
        sha = manifest.tool_source(release)[1] if release in manifest.tools else None
        stacks[name] = {"ref": sref.ref, "sha": sha}
    files, units = _config_files(manifest, node, state_dir, root, home, runner)
    restarts = list(dict.fromkeys([*package_restarts, *units]))
    return {
        "stacks": stacks,
        "roles": {k: list(v) for k, v in prof.roles.items()},
        "files": files,
        "restarts": restarts,
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
