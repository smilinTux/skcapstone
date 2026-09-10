"""Governed remote card dispatch for builder standby nodes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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
MAX_ATTEMPTS = 2
_PROCESSES: dict[str, object] = {}


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
    if not store.actuation_allowed(paths):
        return None
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
            prior = _load(status_path(paths, view.name, card_id)) or {}
            if prior.get("request_id") == existing.get("request_id") and (
                prior.get("state") in TERMINAL_STATES
                and not (
                    prior.get("state") == "failed"
                    and int(prior.get("attempt") or 1) < MAX_ATTEMPTS
                )
            ):
                return None
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
        "writer": {"role": "sknoded", "node": node, "identity": store.writer_identity()},
        **extra,
    }
    path = status_path(paths, node, request["card_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def _send_status(owner: str, request: dict, state: str) -> bool:
    """Send one best-effort, attributable lifecycle notice to coordination."""
    command = os.environ.get("SKMAIL_BIN") or shutil.which("skmail")
    if command is None:
        return False
    try:
        result = subprocess.run(
            [
                command,
                "send",
                owner,
                "jarvis",
                "normal" if state == "completed" else "urgent",
                f"BUILDER-DISPATCH-{request['card_id']}-{state.upper()}",
                f"card={request['card_id']} request_id={request['request_id']} state={state}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _proc_start_ticks(pid: int) -> str | None:
    """Return the Linux process birth token used to fence PID reuse."""
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def _process_state(status: dict) -> tuple[bool | None, int | None]:
    """Return exact-generation liveness, or unknown when death is unproven."""
    request_id = str(status.get("request_id") or "")
    process = _PROCESSES.get(request_id)
    if process is not None:
        code = process.poll()
        if code is None:
            return True, None
        _PROCESSES.pop(request_id, None)
        return False, int(code)
    try:
        pid = int(status["pid"])
        expected = str(status["pid_start_ticks"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not expected:
        return None, None
    current = _proc_start_ticks(pid)
    return (current == expected, None) if current is not None else (False, None)


def _release_exact(
    coordination_home: Path, card_id: str, owner: str, revision: str, *, actor: str
) -> bool:
    """Release only the generation still authoritative in CardStore."""
    card = CardStore(coordination_home).fold(card_id)
    if not card or card.owner != owner or card.meta.get("_claim_revision") != revision:
        return False
    return Board(coordination_home).release_claim(
        owner, card_id, actor=actor, expected_claim_revision=revision
    )


def _frozen_claim_status(
    paths: FleetPaths,
    coordination_home: Path,
    node: str,
    request: dict,
    owner: str,
    revision: str,
    prior_attempt: int,
) -> dict | None:
    """Release an exact prelaunch claim when the human freeze has won."""
    if store.actuation_allowed(paths):
        return None
    released = _release_exact(coordination_home, request["card_id"], owner, revision, actor=owner)
    return _write_status(
        paths,
        node,
        request,
        "frozen" if released else "blocked",
        owner=owner,
        claim_revision=revision,
        attempt=prior_attempt,
        claim_released=released,
    )


def _reconcile_running(
    paths: FleetPaths, coordination_home: Path, node: str, request: dict, status: dict
) -> dict:
    """Refresh one live generation or close it after exact process death."""
    alive, exit_code = _process_state(status)
    owner = str(status.get("owner") or "")
    revision = str(status.get("claim_revision") or "")
    common = {
        "owner": owner,
        "claim_revision": revision,
        "pid": status.get("pid"),
        "pid_start_ticks": status.get("pid_start_ticks"),
        "attempt": int(status.get("attempt") or 1),
    }
    if alive is not False:
        return _write_status(
            paths,
            node,
            request,
            "running",
            **common,
            liveness="live" if alive else "unknown",
        )
    card = CardStore(coordination_home).fold(request["card_id"])
    if card and getattr(card.status, "value", card.status) == "done":
        state = "completed"
        released = False
        completion = {
            "verdict": card.links.get("verdict"),
            "evidence": card.links.get("evidence"),
            "evidence_sha256": card.links.get("evidence_sha256")
            or card.links.get("candidate_evidence_sha256"),
        }
    else:
        released = _release_exact(
            coordination_home, request["card_id"], owner, revision, actor=owner
        )
        state = "failed" if released else "blocked"
        completion = None
    mail_sent = _send_status(owner, request, state)
    return _write_status(
        paths,
        node,
        request,
        state,
        **common,
        exit_code=exit_code,
        claim_released=released,
        mail_sent=mail_sent,
        completion=completion,
    )


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
    if not store.actuation_allowed(paths):
        return None
    directory = paths.root / "dispatch" / node
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        request = _load(path) or {}
        if request.get("schema") != "skfleet.builder-dispatch/v1" or request.get("node") != node:
            continue
        prior = _load(status_path(paths, node, request["card_id"])) or {}
        if prior.get("request_id") == request.get("request_id"):
            if prior.get("state") == "running":
                return _reconcile_running(paths, coordination_home, node, request, prior)
            if prior.get("state") not in {"failed", "frozen"} or (
                prior.get("state") == "failed" and int(prior.get("attempt") or 1) >= MAX_ATTEMPTS
            ):
                continue
        attempt = int(prior.get("attempt") or 0) + 1
        owner = f"pi-builder-standby-{node}-{request['card_id']}"
        workspace = paths.root / "workspaces" / owner
        if not store.actuation_allowed(paths):
            return None
        materializer(request, workspace)
        if not store.actuation_allowed(paths):
            return _write_status(
                paths,
                node,
                request,
                "frozen",
                attempt=int(prior.get("attempt") or 0),
                claim_released=False,
            )
        Board(coordination_home).claim_task(owner, request["card_id"])
        card = CardStore(coordination_home).fold(request["card_id"])
        revision = str(card.meta.get("_claim_revision") or "") if card else ""
        if not card or card.owner != owner or not revision:
            raise BuilderDispatchError("claimed generation is not authoritative")
        frozen = _frozen_claim_status(
            paths,
            coordination_home,
            node,
            request,
            owner,
            revision,
            int(prior.get("attempt") or 0),
        )
        if frozen is not None:
            return frozen
        startup_hello(coordination_home, owner, host=node)
        command = worker_command(request, owner, revision, workspace)
        frozen = _frozen_claim_status(
            paths,
            coordination_home,
            node,
            request,
            owner,
            revision,
            int(prior.get("attempt") or 0),
        )
        if frozen is not None:
            return frozen
        run = launcher or (lambda argv, cwd: subprocess.Popen(argv, cwd=cwd))
        exclusion_acquired = False
        try:
            with store.actuation_exclusion(paths):
                exclusion_acquired = True
                frozen = _frozen_claim_status(
                    paths,
                    coordination_home,
                    node,
                    request,
                    owner,
                    revision,
                    int(prior.get("attempt") or 0),
                )
                if frozen is not None:
                    return frozen
                process = run(command, workspace)
        except Exception:
            released = _release_exact(
                coordination_home, request["card_id"], owner, revision, actor=owner
            )
            state = "failed" if released and exclusion_acquired else "blocked"
            _write_status(
                paths,
                node,
                request,
                state,
                owner=owner,
                claim_revision=revision,
                attempt=attempt if exclusion_acquired else int(prior.get("attempt") or 0),
                claim_released=released,
                mail_sent=_send_status(owner, request, state),
            )
            raise
        pid = getattr(process, "pid", None)
        if pid is not None:
            _PROCESSES[request["request_id"]] = process
        return _write_status(
            paths,
            node,
            request,
            "running",
            owner=owner,
            claim_revision=revision,
            pid=pid,
            pid_start_ticks=_proc_start_ticks(pid) if pid is not None else None,
            attempt=attempt,
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
    alive, _ = _process_state(status)
    if alive is not False:
        return False
    released = _release_exact(coordination_home, card_id, owner, revision, actor="niobe")
    if released:
        _write_status(paths, node, request, "stale", owner=owner, claim_revision=revision)
    return released
