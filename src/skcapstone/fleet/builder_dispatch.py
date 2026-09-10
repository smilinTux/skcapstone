"""Governed remote card dispatch for builder standby nodes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from skcoord.card_store import CardStore

from ..atomic_io import atomic_write_text
from ..coordination import Board
from ..seat_mail import startup_hello
from . import scheduler, store
from .node_controller import NodeView, node_views
from .paths import FleetPaths, valid_name

ROLE = "builder-standby"
PROVIDER = "skgateway"
LEASE_SECONDS = 900
TERMINAL_STATES = {"completed", "blocked", "failed", "stale"}


class BuilderDispatchError(ValueError):
    """A remote dispatch request failed a governance fence."""


def _now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    """Return one stable UTC timestamp."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> dict | None:
    """Read one JSON object, failing closed on malformed content."""
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuilderDispatchError(f"malformed dispatch record: {path}") from exc
    if not isinstance(value, dict):
        raise BuilderDispatchError(f"dispatch record is not an object: {path}")
    return value


def request_path(paths: FleetPaths, node: str, card_id: str) -> Path:
    """Return the scheduler-owned request path."""
    return paths.root / "dispatch" / node / f"{card_id}.json"


def status_path(paths: FleetPaths, node: str, card_id: str) -> Path:
    """Return the node-owned dispatch status path."""
    return paths.status_path(node, "dispatch", card_id)


def eligible(core: dict, labels: list[str] | tuple[str, ...]) -> bool:
    """Return whether a card is the bounded generic medium source workload."""
    normalized = {str(label).strip().lower() for label in labels}
    return (
        "sk-m" in normalized
        and "source-only" in normalized
        and not any(label.startswith("seat-") for label in normalized)
        and not normalized.intersection({"host-pin", "codex-only", "qwen-only", "glm-only"})
        and isinstance(core.get("id"), str)
    )


def _source(core: dict) -> tuple[str, str, str]:
    """Read and validate exact credential-free source metadata."""
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    repository = str(meta.get("repository") or "").strip()
    base_ref = str(meta.get("base_ref") or "").strip()
    revision = str(meta.get("base_revision") or "").strip().lower()
    if not repository.startswith("https://") or "@" in repository:
        raise BuilderDispatchError("repository must be credential-free https")
    if not base_ref or len(base_ref) > 160 or any(ch.isspace() for ch in base_ref):
        raise BuilderDispatchError("base_ref is missing or unsafe")
    if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
        raise BuilderDispatchError("base_revision must be exact 40-hex")
    return repository, base_ref, revision


def _ready_builders(paths: FleetPaths) -> list[NodeView]:
    """Return current Ready, actuating builder standby nodes."""
    result = []
    for view in node_views(paths):
        spec = store.read_spec(paths, "node", view.name) or {}
        if view.role == ROLE and spec.get("spec", {}).get("actuate") is True:
            result.append(view)
    return result


def _node_busy(paths: FleetPaths, node: str) -> bool:
    """Return whether a node has one nonterminal remote dispatch."""
    directory = paths.root / "dispatch" / node
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        request = _load(path) or {}
        status = _load(status_path(paths, node, str(request.get("card_id") or ""))) or {}
        if (
            status.get("request_id") == request.get("request_id")
            and status.get("state") in TERMINAL_STATES
        ):
            continue
        return True
    return False


def offer(
    paths: FleetPaths,
    core: dict,
    labels: list[str] | tuple[str, ...],
    *,
    writer: store.Writer,
    now: datetime | None = None,
) -> dict | None:
    """Place and publish one idempotent Niobe-owned builder dispatch."""
    if writer.role != "scheduler" or writer.node != "niobe":
        raise BuilderDispatchError("only the Niobe scheduler may offer builder work")
    if not eligible(core, labels):
        return None
    card_id = core["id"].lower()
    if not valid_name(card_id):
        raise BuilderDispatchError("invalid card id")
    repository, base_ref, revision = _source(core)
    ready = _ready_builders(paths)
    for view in ready:
        existing = _load(request_path(paths, view.name, card_id))
        if existing and existing.get("base_revision") == revision:
            return existing
    builders = [view for view in ready if not _node_busy(paths, view.name)]
    decision = scheduler.select(builders, scheduler.Workload("job", card_id))
    if decision.node is None:
        return None
    scheduler.place(paths, scheduler.Workload("job", card_id), writer=writer, views=builders)
    stamp = now or _now()
    identity = f"{card_id}\0{decision.node}\0{revision}"
    request = {
        "schema": "skfleet.builder-dispatch/v1",
        "request_id": hashlib.sha256(identity.encode()).hexdigest(),
        "card_id": card_id,
        "node": decision.node,
        "role": ROLE,
        "provider": PROVIDER,
        "repository": repository,
        "base_ref": base_ref,
        "base_revision": revision,
        "offered_at": _iso(stamp),
        "lease_expires_at": _iso(stamp + timedelta(seconds=LEASE_SECONDS)),
        "writer": {"role": writer.role, "node": writer.node, "identity": writer.identity},
    }
    path = request_path(paths, decision.node, card_id)
    existing = _load(path)
    if existing and existing.get("request_id") == request["request_id"]:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(request, indent=2, sort_keys=True) + "\n")
    return request


def _write_status(paths: FleetPaths, node: str, request: dict, state: str, **extra) -> dict:
    """Write one node-attributed state for an exact request generation."""
    payload = {
        "schema": "skfleet.builder-dispatch-status/v1",
        "request_id": request["request_id"],
        "card_id": request["card_id"],
        "node": node,
        "state": state,
        "heartbeat_at": _iso(_now()),
        **extra,
    }
    path = status_path(paths, node, request["card_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def worker_command(request: dict, owner: str, claim_revision: str, workspace: Path) -> list[str]:
    """Build the only permitted remote worker command."""
    return [
        "env",
        f"SKAGENT={owner}",
        f"SKCAPSTONE_AGENT={owner}",
        f"SKFLEET_CARD_ID={request['card_id']}",
        f"SKFLEET_CLAIM_REVISION={claim_revision}",
        os.environ.get("SKFLEET_PI", str(Path.home() / ".npm-global/bin/pi")),
        "--approve",
        "--name",
        owner,
        "--provider",
        PROVIDER,
        "--model",
        "pi",
        "--thinking",
        "off",
        "--no-context-files",
        "-p",
        (
            f"Work only SKCapstone card {request['card_id']}. "
            f"Claim revision {claim_revision}. Source is reconstructed at {workspace}. "
            "Use skcapstone coord and SKMail for all lifecycle updates."
        ),
    ]


def materialize_source(request: dict, workspace: Path) -> Path:
    """Reconstruct and verify the exact source revision before claiming."""
    if workspace.exists():
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True
        )
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if (
            head.returncode == 0
            and head.stdout.strip() == request["base_revision"]
            and remote.returncode == 0
            and remote.stdout.strip() == request["repository"]
        ):
            return workspace
        raise BuilderDispatchError("existing workspace does not match exact source binding")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{workspace.name}.", dir=workspace.parent))
    try:
        commands = (
            ["git", "init", "--quiet", str(temporary)],
            ["git", "remote", "add", "origin", request["repository"]],
            ["git", "fetch", "--quiet", "--depth=1", "origin", request["base_revision"]],
            ["git", "checkout", "--quiet", "--detach", request["base_revision"]],
        )
        for command in commands:
            result = subprocess.run(command, cwd=temporary, capture_output=True, text=True)
            if result.returncode:
                raise BuilderDispatchError("exact source reconstruction failed")
        temporary.replace(workspace)
        return workspace
    except Exception:
        if temporary.exists():
            import shutil

            shutil.rmtree(temporary)
        raise


def consume_one(
    paths: FleetPaths,
    coordination_home: Path,
    node: str,
    *,
    launcher: Callable[[list[str], Path], object] | None = None,
    materializer: Callable[[dict, Path], Path] = materialize_source,
) -> dict | None:
    """Claim and launch one exact request on its assigned builder node."""
    spec = store.read_spec(paths, "node", node) or {}
    if spec.get("spec", {}).get("role") != ROLE or spec.get("spec", {}).get("actuate") is not True:
        return None
    directory = paths.root / "dispatch" / node
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        request = _load(path) or {}
        if request.get("schema") != "skfleet.builder-dispatch/v1" or request.get("node") != node:
            continue
        prior = _load(status_path(paths, node, request["card_id"])) or {}
        if prior.get("request_id") == request.get("request_id"):
            return None
        owner = f"pi-builder-standby-{node}-{request['card_id']}"
        workspace = Path.home() / ".skcapstone/fleet/workspaces" / owner
        materializer(request, workspace)
        Board(coordination_home).claim_task(owner, request["card_id"])
        card = CardStore(coordination_home).fold(request["card_id"])
        revision = str(card.meta.get("_claim_revision") or "") if card else ""
        if not card or card.owner != owner or not revision:
            raise BuilderDispatchError("claimed generation is not authoritative")
        startup_hello(coordination_home, owner, host=node)
        command = worker_command(request, owner, revision, workspace)
        run = launcher or (lambda argv, cwd: subprocess.Popen(argv, cwd=cwd))
        try:
            process = run(command, workspace)
        except Exception:
            _write_status(paths, node, request, "failed", owner=owner, claim_revision=revision)
            raise
        return _write_status(
            paths,
            node,
            request,
            "running",
            owner=owner,
            claim_revision=revision,
            pid=getattr(process, "pid", None),
        )
    return None


def recover_stale(
    paths: FleetPaths,
    coordination_home: Path,
    node: str,
    card_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Release only the exact expired generation reported by this node."""
    request = _load(request_path(paths, node, card_id)) or {}
    status = _load(status_path(paths, node, card_id)) or {}
    if not request or status.get("request_id") != request.get("request_id"):
        return False
    if status.get("state") != "running":
        return False
    try:
        heartbeat = datetime.strptime(status["heartbeat_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (KeyError, TypeError, ValueError):
        return False
    if (now or _now()) <= heartbeat + timedelta(seconds=LEASE_SECONDS):
        return False
    owner = str(status.get("owner") or "")
    revision = str(status.get("claim_revision") or "")
    card = CardStore(coordination_home).fold(card_id)
    if not card or card.owner != owner or card.meta.get("_claim_revision") != revision:
        return False
    released = Board(coordination_home).release_claim(
        owner, card_id, actor="niobe", expected_claim_revision=revision
    )
    if released:
        _write_status(paths, node, request, "stale", owner=owner, claim_revision=revision)
    return released
