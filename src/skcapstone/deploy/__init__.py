"""Deploy released builds of main, never a working tree.

Production on a node is the venv at ``~/.skenv`` plus the units, MCP servers
and timers that run from it. This package keeps that equal to a release
manifest: one YAML file in a git repository of its own, changed only by PR.

* :mod:`.manifest`  parse and validate the release set
* :mod:`.inspect`   read what the node actually runs
* :mod:`.drift`     findings for ``doctor`` and the drift timer
* :mod:`.rollout`   pre-flight, build, verify, switch, restart, roll back

Where the manifest comes from, first match wins:

1. ``--manifest PATH`` / ``SK_DEPLOY_MANIFEST`` (a local file, for tests and
   emergencies; its id is the file's sha256);
2. ``SK_DEPLOY_MANIFEST_URL=<git url>`` (any URL git can clone, for example
   an ssh host alias bound to a read-only deploy key), else
   ``SK_DEPLOY_MANIFEST_REPO=owner/name`` (``https://github.com/owner/name``).
   The repo (file ``manifest.yaml``) is cloned into the host-local state dir
   and always read at ``origin/main``, so only a merged manifest can deploy.
   ``--ref`` picks an earlier commit of main for a deliberate rollback, and
   is refused if it is not on main. One manifest repo per cluster: a node
   names only its own cluster's repo.

A manifest may include public stacks (``stacks:``, see :mod:`.stack`). Each
one's ``stack.yaml`` is read from a bare mirror under ``<state>/stacks/`` at
its ref, which must be on the stack repo's main, and merged in. A node may
pin a stack to another ref (``nodes.<node>.stacks``), so the merged manifest
belongs to one node: pass ``node`` (this host by default). A manifest without
``stacks:`` is read exactly as before and fetches nothing more.
"""

from __future__ import annotations

import copy
import hashlib
import os
import socket
import subprocess
from pathlib import Path
from typing import Optional

import yaml

from .manifest import Manifest, load_manifest

MANIFEST_FILE = "manifest.yaml"


class ManifestUnavailableError(RuntimeError):
    """No manifest is configured or it could not be fetched."""


def node_config(path: Optional[Path] = None) -> dict[str, str]:
    """``KEY=VALUE`` lines from the host-local ``~/.config/sk-deploy/env``.

    The same file is the drift timer's ``EnvironmentFile``, so bash, cron and
    systemd all see one answer without the public code naming a repository.
    """
    path = path or Path.home() / ".config" / "sk-deploy" / "env"
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return out


def manifest_url(conf: Optional[dict[str, str]] = None) -> Optional[str]:
    """The manifest repo's git URL, or ``None`` when none is configured.

    The environment wins over ``~/.config/sk-deploy/env`` (so a one-off
    override on the command line always takes effect); within each,
    ``SK_DEPLOY_MANIFEST_URL`` wins over ``SK_DEPLOY_MANIFEST_REPO``.

    Args:
        conf: The node config (read from disk when omitted).

    Returns:
        A URL git can clone.
    """
    conf = node_config() if conf is None else conf
    for source in (os.environ, conf):
        if source.get("SK_DEPLOY_MANIFEST_URL"):
            return source["SK_DEPLOY_MANIFEST_URL"]
        if source.get("SK_DEPLOY_MANIFEST_REPO"):
            return f"https://github.com/{source['SK_DEPLOY_MANIFEST_REPO']}.git"
    return None


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120, check=False
    )
    if proc.returncode != 0:
        raise ManifestUnavailableError(f"git {' '.join(args)}: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip()


def _profile_key(data: dict, node: Optional[str]) -> Optional[str]:
    """The ``nodes`` key *node* resolves to (as :meth:`Manifest.profile` does), or None."""
    nodes = data.get("nodes") or {}
    name = node or socket.gethostname()
    if name in nodes:
        return name
    return "default" if "default" in nodes else None


def _stack_tool_names(data: dict, state_dir: Path, fetch: bool) -> set[str]:
    """Every tool the manifest's stacks bring, as far as their mirrors can tell (error path)."""
    from . import stack as stack_mod
    from .manifest import StackRef
    from .tools import STACK_RELEASE_SUFFIX, ToolError

    out: set[str] = set()
    for name, raw in (data.get("stacks") or {}).items():
        out.add(name + STACK_RELEASE_SUFFIX)
        try:
            sref = StackRef.model_validate(raw)
            stack, _sha = stack_mod.fetch_stack(name, sref, sref.ref, state_dir / "stacks", fetch)
        except (ToolError, ValueError):
            continue
        out.update(stack.tools)
    return out


def _refuse_stack_refs(manifest: Manifest) -> None:
    """``@STACK:<name>@`` left in a manifest whose node takes no stack names no stack."""
    from .tools import _STACK_REF

    texts = [("smoke", c) for c in manifest.smoke]
    texts += [(f"service {u} smoke", s.smoke or "") for u, s in manifest.services.items()]
    for name, tool in manifest.tools.items():
        texts += [(f"tool {name} setup", a) for a in tool.setup or []]
        texts += [(f"tool {name} setup_env", v) for v in (tool.setup_env or {}).values()]
    for where, text in texts:
        m = _STACK_REF.search(text)
        if m:
            raise ValueError(f"unknown stack {m.group(1)} in {where}")


def _with_stacks(
    manifest_path: Path, state_dir: Path, node: Optional[str], fetch: bool
) -> Manifest:
    """Load *manifest_path* and merge the stacks *node* takes, as *node* sees them.

    Only the stacks named in the node's ``roles`` are fetched and merged (with
    its ``stacks`` canary pins for those): a node that takes no stack resolves
    exactly as if the manifest named none, so a stack whose ref is unreachable
    or bad never takes down a node that does not run it. Profiles of other
    nodes that take a stack this node does not are left out of the merged
    manifest, which belongs to this node alone.
    """
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("stacks"):
        return load_manifest(manifest_path)
    from . import stack as stack_mod
    from .tools import ToolError

    data = copy.deepcopy(data)
    # Role tools join a node's list only in the merge: its configs for them
    # are checked on the merged manifest.
    try:
        manifest = Manifest.model_validate(data, context={"stacks_pending": True})
    except ValueError:
        names = _stack_tool_names(data, state_dir, fetch)
        Manifest.model_validate(data, context={"stacks_pending": True, "stack_tools": names})
        raise
    key = _profile_key(data, node)
    prof = manifest.profile(node)
    taken = set(prof.roles)
    dropped = set(manifest.stacks) - taken
    data["stacks"] = {n: v for n, v in data["stacks"].items() if n in taken}
    nodes = data.get("nodes") or {}
    kept: dict = {}
    for name, raw in nodes.items():
        raw = dict(raw or {})
        if name == key:
            if raw.get("stacks"):
                raw["stacks"] = {s: r for s, r in raw["stacks"].items() if s in taken}
        elif set(raw.get("roles") or {}) & dropped or set(raw.get("stacks") or {}) & dropped:
            continue
        kept[name] = raw
    if "nodes" in data:
        data["nodes"] = kept
    if not taken:
        data.pop("stacks")
        merged = Manifest.model_validate(data)
        _refuse_stack_refs(merged)
        return merged
    files, shas = {}, {}
    for name, sref in manifest.stacks.items():
        if name not in taken:
            continue
        at = prof.stacks.get(name, sref.ref)
        try:
            files[name], shas[name] = stack_mod.fetch_stack(
                name, sref, at, state_dir / "stacks", fetch
            )
        except ToolError as exc:
            raise ManifestUnavailableError(str(exc)) from exc
        data["stacks"][name]["ref"] = at
    return stack_mod.merge_stacks(data, files, shas)


def resolve_manifest(
    state_dir: Path,
    path: Optional[str] = None,
    ref: Optional[str] = None,
    fetch: bool = True,
    node: Optional[str] = None,
) -> tuple[Manifest, str]:
    """Load the release manifest and its id.

    Args:
        state_dir: Host-local deploy state directory (holds the clone).
        path: Explicit manifest file (overrides the repository).
        ref: A commit of the manifest repo's main to use instead of its tip.
        fetch: Fetch the repository (and any stack mirrors) first (off for
            offline doctor runs).
        node: The node the manifest is merged for (a node may pin a stack to
            a canary ref). This hostname by default.

    Returns:
        The manifest and an id naming exactly which manifest it is.

    Raises:
        ManifestUnavailableError: Nothing configured, the fetch failed, or a
            stack ref does not resolve or is not on the stack repo's main.
    """
    conf = node_config()
    path = path or os.environ.get("SK_DEPLOY_MANIFEST") or conf.get("SK_DEPLOY_MANIFEST")
    if path:
        data = Path(path).expanduser().read_bytes()
        return (
            _with_stacks(Path(path).expanduser(), state_dir, node, fetch),
            "file-" + hashlib.sha256(data).hexdigest()[:12],
        )
    url = manifest_url(conf)
    if not url:
        raise ManifestUnavailableError(
            "set SK_DEPLOY_MANIFEST_URL (git url) or SK_DEPLOY_MANIFEST_REPO (owner/name),"
            " or pass --manifest"
        )
    clone = state_dir / "manifest-repo"
    if not (clone / ".git").exists():
        if not fetch:
            raise ManifestUnavailableError(f"{clone} not cloned yet")
        state_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--quiet", url, str(clone)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            raise ManifestUnavailableError(f"clone {url}: {proc.stderr.strip()[-300:]}")
    elif fetch:
        if _git(clone, "remote", "get-url", "origin") != url:
            # The node was pointed at another manifest repo: follow it, never
            # keep reading the old one.
            _git(clone, "remote", "set-url", "origin", url)
        _git(clone, "fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
    tip = _git(clone, "rev-parse", "origin/main")
    commit = tip
    if ref:
        commit = _git(clone, "rev-parse", f"{ref}^{{commit}}")
        try:
            _git(clone, "merge-base", "--is-ancestor", commit, tip)
        except ManifestUnavailableError as exc:
            raise ManifestUnavailableError(f"{ref} is not on the manifest repo's main") from exc
    text = _git(clone, "show", f"{commit}:{MANIFEST_FILE}")
    tmp = state_dir / f".manifest-{commit[:12]}.yaml"
    tmp.write_text(text + "\n", encoding="utf-8")
    try:
        return _with_stacks(tmp, state_dir, node, fetch), commit
    finally:
        tmp.unlink(missing_ok=True)


__all__ = [
    "Manifest",
    "ManifestUnavailableError",
    "load_manifest",
    "manifest_url",
    "resolve_manifest",
]
