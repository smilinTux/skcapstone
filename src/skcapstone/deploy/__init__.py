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
2. ``SK_DEPLOY_MANIFEST_REPO=owner/name`` (default file ``manifest.yaml``),
   cloned into the host-local state dir and always read at ``origin/main``,
   so only a merged manifest can deploy. ``--ref`` picks an earlier commit
   of main for a deliberate rollback, and is refused if it is not on main.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Optional

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


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120, check=False
    )
    if proc.returncode != 0:
        raise ManifestUnavailableError(f"git {' '.join(args)}: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip()


def resolve_manifest(
    state_dir: Path,
    path: Optional[str] = None,
    ref: Optional[str] = None,
    fetch: bool = True,
) -> tuple[Manifest, str]:
    """Load the release manifest and its id.

    Args:
        state_dir: Host-local deploy state directory (holds the clone).
        path: Explicit manifest file (overrides the repository).
        ref: A commit of the manifest repo's main to use instead of its tip.
        fetch: Fetch the repository first (off for offline doctor runs).

    Returns:
        The manifest and an id naming exactly which manifest it is.

    Raises:
        ManifestUnavailableError: Nothing configured, or the fetch failed.
    """
    conf = node_config()
    path = path or os.environ.get("SK_DEPLOY_MANIFEST") or conf.get("SK_DEPLOY_MANIFEST")
    if path:
        data = Path(path).expanduser().read_bytes()
        return (
            load_manifest(Path(path).expanduser()),
            "file-" + hashlib.sha256(data).hexdigest()[:12],
        )
    repo = os.environ.get("SK_DEPLOY_MANIFEST_REPO") or conf.get("SK_DEPLOY_MANIFEST_REPO")
    if not repo:
        raise ManifestUnavailableError(
            "set SK_DEPLOY_MANIFEST_REPO (owner/name) or pass --manifest"
        )
    clone = state_dir / "manifest-repo"
    if not (clone / ".git").exists():
        if not fetch:
            raise ManifestUnavailableError(f"{clone} not cloned yet")
        state_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--quiet", f"https://github.com/{repo}.git", str(clone)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            raise ManifestUnavailableError(f"clone {repo}: {proc.stderr.strip()[-300:]}")
    elif fetch:
        _git(clone, "fetch", "--quiet", "origin", "main")
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
        return load_manifest(tmp), commit
    finally:
        tmp.unlink(missing_ok=True)


__all__ = ["Manifest", "ManifestUnavailableError", "load_manifest", "resolve_manifest"]
