"""Disabled-by-default, chiap08-only Cursor-plan producer routing and launch."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

HOST = "chiap08"
# Deliberately finite: new vendor aliases require review, never prefix matching.
MODELS = frozenset({"composer-2.5", "cursor-grok-4.6-high"})
SIZES = {"sk-s": "composer-2.5", "sk-m": "composer-2.5", "sk-l": "cursor-grok-4.6-high"}


def require_model(model: str) -> str:
    """Refuse defaults, parameters, other-quota models and unreviewed aliases."""
    if model not in MODELS:
        raise ValueError("Cursor model is outside the fleet plan allowlist")
    return model


def candidate(policy: dict, route: str, labels, *, host: str = HOST) -> dict | None:
    """Offer only public source producers without conflicting family requirements."""
    lane = policy.get("lanes", {}).get("cursor")
    labels = set(labels)
    if (
        lane != {"enabled": True, "provider": "cursor", "host": HOST}
        or host != HOST
        or route not in SIZES
        or "source-only" not in labels
        or labels & {"private", "sensitive", "protected-data", "human-gate", "qwen-first"}
        or any(label.startswith("seat-") for label in labels)
        or any(
            label.endswith("-only") and label not in {"source-only", "cursor-only"}
            for label in labels
        )
    ):
        return None
    return dict(
        family="cursor",
        provider_family="cursor",
        provider="cursor",
        model=require_model(SIZES[route]),
        gateway_backend="cursor",
        capacity_domain="cursor",
        size_class=route[3:].upper(),
        policy_tier="public",
    )


def runtime() -> tuple[Path, Path]:
    """Resolve only the installed local CLI and the selected private account auth."""
    from .native_codex import _path

    executable = (Path.home() / ".local/bin/cursor-agent").resolve(strict=True)
    _path(executable)
    _path(executable.parent / "node")
    _path(executable.parent / "index.js")
    auth = _path(Path.home() / ".config/cursor/auth.json", private=True)
    return executable, auth


def preflight(policy: dict, binding: dict) -> dict:
    """Check local custody without any model inference, gateway probe or fallback."""
    expected = candidate(
        policy, "sk-" + binding["size_class"].lower(), ["source-only"], host=binding.get("host")
    )
    if (
        not expected
        or any(binding.get(k) != v for k, v in expected.items())
        or socket.gethostname().split(".")[0].lower() != HOST
    ):
        raise ValueError("Cursor route or execution host changed")
    executable, _ = runtime()
    if not Path("/usr/bin/bwrap").is_file():
        raise ValueError("Cursor requires bwrap")
    from .production_builder import review_wrapper_path

    review_wrapper_path()
    return {
        "provider": "cursor",
        "requested_identity": binding["model"],
        "runtime": str(executable),
        "probe": "local-only",
    }


def worker_command(request: dict, owner: str, claim: str, workspace: Path) -> list[str]:
    """Reuse managed worker evidence, heartbeat and the builder's resource admission."""
    from .production_brief import _identity
    from .production_builder import review_child_command, review_wrapper_path
    from .worker_git import identity

    _identity(request["card_id"], owner, claim)
    bound = request["production"]
    expected = candidate(
        {"lanes": {"cursor": {"enabled": True, "provider": "cursor", "host": HOST}}},
        request["logical_route"],
        request["labels"],
        host=bound["host"],
    )
    if expected is None or any(bound.get(k) != v for k, v in expected.items()):
        raise ValueError("Cursor worker route changed")
    executable, _ = runtime()
    home = Path(os.environ.get("SKCAPSTONE_HOME", str(Path.home() / ".skcapstone")))
    logs = home / "fleet/cursor-logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = [
        "/usr/bin/env",
        "-i",
        "HOME=" + str(Path.home()),
        "PATH=" + str(Path.home() / ".skenv/bin") + ":/usr/bin:/bin",
        "LANG=C.UTF-8",
        "XDG_RUNTIME_DIR=/run/user/" + str(os.getuid()),
        "SKCAPSTONE_HOME=" + str(home),
        "SKAGENT=" + owner,
        "SKCAPSTONE_AGENT=" + owner,
        "SKFLEET_CARD_ID=" + request["card_id"],
        "SKFLEET_CLAIM_REVISION=" + claim,
        "SKFLEET_SESSION_ID=" + owner,
        "SKFLEET_PRODUCTION_POLICY=" + os.environ["SKFLEET_PRODUCTION_POLICY"],
        "SKFLEET_AUTHORITY_HOST=" + os.environ["SKFLEET_AUTHORITY_HOST"],
        *(k + "=" + v for k, v in identity(owner).items()),
    ]
    child = [
        *environment,
        sys.executable,
        "-m",
        "skcapstone.fleet.production_cursor_worker",
        "--request",
        json.dumps(request, sort_keys=True),
        "--workspace",
        str(workspace),
    ]
    return [
        *environment,
        sys.executable,
        review_wrapper_path(),
        "--card",
        request["card_id"],
        "--owner",
        owner,
        "--claim-revision",
        claim,
        "--host",
        HOST,
        "--lane",
        "cursor",
        "--model",
        require_model(bound["model"]),
        "--provider",
        "cursor",
        "--capacity-domain",
        "cursor",
        "--logical-route",
        request["logical_route"],
        "--source-repository",
        request["repository"],
        "--source-base-revision",
        request["base_revision"],
        "--stdout",
        str(logs / (claim + ".jsonl")),
        "--evidence-dir",
        str(home / "evidence/worker-exits"),
        "--live-snapshot",
        str(home / "fleet/live" / (HOST + ".json")),
        "--session",
        owner,
        "--worker-executable",
        str(executable.parent / "index.js"),
        "--",
        *review_child_command(home, owner, request["card_id"], claim, child),
    ]
