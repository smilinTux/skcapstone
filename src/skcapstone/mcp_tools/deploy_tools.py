"""Deploy status tool — platform detection, secrets backend, last deploy, ArgoCD sync."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from mcp.types import TextContent, Tool

from ._helpers import _json_response

TOOLS: list[Tool] = [
    Tool(
        name="deploy_status",
        description=(
            "Report infrastructure deployment status: detected platform "
            "(swarm/k8s/rke2 from skstacks/v2/ layout), active secrets "
            "backend (SKSTACKS_SECRET_BACKEND env), last deploy commit "
            "(git log --oneline -1), and ArgoCD app-of-apps sync/health "
            "when app-of-apps.yaml is present. Returns structured JSON."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "skstacks_root": {
                    "type": "string",
                    "description": (
                        "Absolute path to skstacks/v2/. "
                        "Auto-detected from git root or SKSTACKS_V2_ROOT env if omitted."
                    ),
                },
            },
            "required": [],
        },
    ),
]


# ── Internal helpers ──────────────────────────────────────────


def _git_root() -> Optional[Path]:
    """Return the git repository root, or None if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _find_skstacks_v2(override: Optional[str] = None) -> Optional[Path]:
    """Locate the skstacks/v2/ directory.

    Resolution order:
    1. *override* argument (caller-supplied path)
    2. ``SKSTACKS_V2_ROOT`` environment variable
    3. ``<git-root>/skstacks/v2/``
    4. ``<cwd>/skstacks/v2/``
    """
    if override:
        p = Path(override).expanduser()
        return p if p.exists() else None

    env_val = os.environ.get("SKSTACKS_V2_ROOT")
    if env_val:
        p = Path(env_val).expanduser()
        return p if p.exists() else None

    git_root = _git_root()
    if git_root:
        candidate = git_root / "skstacks" / "v2"
        if candidate.exists():
            return candidate

    cwd_candidate = Path.cwd() / "skstacks" / "v2"
    if cwd_candidate.exists():
        return cwd_candidate

    return None


def _detect_platforms(v2_root: Path) -> list[str]:
    """Return detected platforms from skstacks/v2/ structure.

    * ``rke2``  — ``platform/rke2/`` directory present
    * ``swarm`` — any ``docker-compose*.j2`` file found recursively
    * ``k8s``   — ``overlays/`` directory present
    """
    detected: list[str] = []

    if (v2_root / "platform" / "rke2").exists():
        detected.append("rke2")

    if list(v2_root.rglob("docker-compose*.j2")):
        detected.append("swarm")

    if (v2_root / "overlays").exists():
        detected.append("k8s")

    return detected or ["unknown"]


def _active_secrets_backend() -> str:
    """Read ``SKSTACKS_SECRET_BACKEND`` env var (default: ``vault-file``)."""
    return os.environ.get("SKSTACKS_SECRET_BACKEND", "vault-file")


def _last_deploy_commit(repo_root: Optional[Path] = None) -> str:
    """Return ``git log --oneline -1`` from *repo_root* (or CWD)."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(repo_root) if repo_root else None,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return f"error: {result.stderr.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"error: {exc}"


def _argocd_sync_status(v2_root: Path) -> Optional[dict]:
    """Return ArgoCD sync/health for app-of-apps if the manifest exists.

    Runs ``kubectl get application app-of-apps -n argocd`` and extracts
    ``.status.sync.status`` + ``.status.health.status``.  When kubectl is
    unavailable or the cluster is unreachable the error is returned in the
    dict rather than raising.
    """
    aoa_path = v2_root / "cicd" / "argocd" / "app-of-apps.yaml"
    if not aoa_path.exists():
        return None

    base: dict = {"app": "app-of-apps", "manifest": str(aoa_path)}
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "application", "app-of-apps",
                "-n", "argocd",
                "-o", "jsonpath={.status.sync.status} {.status.health.status}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            return {
                **base,
                "sync": parts[0] if parts else "Unknown",
                "health": parts[1] if len(parts) > 1 else "Unknown",
            }
        return {**base, "error": result.stderr.strip() or "kubectl returned non-zero"}
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {**base, "error": str(exc)}


# ── Handler ───────────────────────────────────────────────────


async def _handle_deploy_status(args: dict) -> list[TextContent]:
    """Return deployment status: platform, secrets backend, last commit, ArgoCD."""
    v2_root = _find_skstacks_v2(args.get("skstacks_root"))
    repo_root = _git_root()

    if v2_root is None:
        return _json_response({
            "platforms": ["unknown"],
            "skstacks_v2": None,
            "secrets_backend": _active_secrets_backend(),
            "last_deploy": _last_deploy_commit(repo_root),
            "argocd": None,
            "warning": (
                "skstacks/v2/ not found; "
                "set SKSTACKS_V2_ROOT or run from repo root"
            ),
        })

    return _json_response({
        "platforms": _detect_platforms(v2_root),
        "skstacks_v2": str(v2_root),
        "secrets_backend": _active_secrets_backend(),
        "last_deploy": _last_deploy_commit(repo_root),
        "argocd": _argocd_sync_status(v2_root),
    })


HANDLERS: dict = {
    "deploy_status": _handle_deploy_status,
}
