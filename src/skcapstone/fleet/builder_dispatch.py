"""Governed remote card dispatch for builder standby nodes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from skcoord.card_store import CardStore
from skcoord.coordination import TaskUnclaimable

from ..atomic_io import atomic_write_text
from ..coordination import Board
from ..seat_mail import startup_hello
from . import scheduler, store
from .churn_breaker import ClaimRefusedError, assert_claim_permitted
from .node_controller import NodeView, node_views
from .paths import SOVEREIGN_HOME, FleetPaths, valid_name

ROLE = "builder-standby"
PROVIDER = "skgateway"
LOGICAL_ROUTES = frozenset({"sk-s", "sk-m", "sk-l", "sk-xl"})
LEASE_SECONDS = 900
TERMINAL_STATES = {"completed", "blocked", "failed", "stale"}
MAX_ATTEMPTS = 2
MATCH_RETRY_LIMIT = 3
BUILDER_CAPACITY = 4
_PROCESSES: dict[str, object] = {}
_WORKER_TOOLS = "read,bash,edit,write,grep,find,ls"
_BUNDLED_GUARD = Path(__file__).resolve().parents[3] / "scripts/fleet/pi-cardstore-guard.mjs"

logger = logging.getLogger(__name__)


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


@contextmanager
def _request_exclusion(path: Path):
    """Serialize one request generation across duplicate node daemons."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def logical_route(labels: list[str] | tuple[str, ...]) -> str | None:
    """Return the card's one unambiguous governed logical route."""
    routes = LOGICAL_ROUTES.intersection(str(label).strip().lower() for label in labels)
    return next(iter(routes)) if len(routes) == 1 else None


def eligible(core: dict, labels: list[str] | tuple[str, ...]) -> bool:
    """Return whether a card is a bounded provider-neutral source workload."""
    normalized = {str(label).strip().lower() for label in labels}
    return (
        logical_route(labels) is not None
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


def _request_matches_current_card(coordination_home: Path, request: dict) -> None:
    """Reject an offered request after any source or eligibility amendment."""
    card = CardStore(coordination_home).fold(str(request.get("card_id") or ""))
    if card is None:
        raise BuilderDispatchError("offered card is no longer foldable")
    core = {"id": card.id, "meta": card.meta}
    labels = sorted(str(label).strip().lower() for label in card.labels)
    expected_labels = request.get("labels")
    if (
        not eligible(core, labels)
        or _source(core)
        != (request.get("repository"), request.get("base_ref"), request.get("base_revision"))
        or labels != expected_labels
    ):
        raise BuilderDispatchError("offered card changed after dispatch request")


def _ensure_request_matches_current_card(coordination_home: Path, request: dict) -> None:
    """Re-fold a request briefly before treating a mismatch as durable."""
    for check in range(MATCH_RETRY_LIMIT):
        try:
            _request_matches_current_card(coordination_home, request)
            return
        except BuilderDispatchError:
            if check == MATCH_RETRY_LIMIT - 1:
                raise


def _ready_builders(paths: FleetPaths) -> list[NodeView]:
    """Return current Ready, actuating builder standby nodes."""
    result = []
    for view in node_views(paths):
        spec = store.read_spec(paths, "node", view.name) or {}
        if view.role == ROLE and spec.get("spec", {}).get("actuate") is True:
            result.append(view)
    return result


def _node_load(paths: FleetPaths, node: str) -> int:
    """Return the number of nonterminal remote dispatches on one node."""
    load = 0
    directory = paths.root / "dispatch" / node
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        request = _load(path) or {}
        status = _load(status_path(paths, node, str(request.get("card_id") or ""))) or {}
        if (
            status.get("request_id") == request.get("request_id")
            and status.get("state") in TERMINAL_STATES
        ):
            continue
        load += 1
    return load


def _lease_expired(request: dict, now: datetime) -> bool:
    """Return whether an unanswered offer's lease has run out.

    Mirrors the consumer's own reading exactly (``_consume_available`` treats a
    missing or unparseable ``lease_expires_at`` as ``datetime.min`` and blocks
    the request): a lease this scheduler cannot parse is one the node will
    never honour, so it is expired here too rather than holding the card
    against a run that can no longer happen.
    """
    try:
        expires = datetime.strptime(request["lease_expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (KeyError, TypeError, ValueError):
        return True
    return now > expires


def request_holds_card(request: dict, status: dict, now: datetime) -> bool:
    """Return whether one request/status pair means the builder holds the card.

    This is the whole admission contract in one predicate. A builder-eligible
    card is withheld from the local lanes ONLY while the builder path actively
    holds it, which is exactly two situations:

    * the node has answered this exact request generation with a non-terminal
      state (``accepted``/``running``/``frozen``), so a worker is on it or is
      about to be; or
    * the node has not answered this generation at all and the offer lease has
      not yet expired, which is the window between Niobe writing the request
      and the remote claim event reaching this host over Syncthing.

    Everything else means the builder is NOT holding the card: a terminal
    status (``completed``/``blocked``/``failed``/``stale``) and an offer that
    expired unclaimed both leave the card unowned. Before 2026-09-19 those
    cards were withheld anyway, because the rotation withheld on
    ``eligible()`` alone: measured on chi, ``026a08d9`` (terminal
    ``failed``/attempt 2) and ``23554ec7`` (a binding no offer can even be
    written for) were held away from 54 free local seats by a builder that
    could never take either of them.

    Args:
        request: One dispatch request record, or {} when none exists.
        status: The paired dispatch status record, or {} when none exists.
        now: The instant to judge the offer lease against.

    Returns:
        True when the builder path is actively holding this card.
    """
    if not request:
        return False
    if status.get("request_id") == request.get("request_id"):
        return status.get("state") not in TERMINAL_STATES
    return not _lease_expired(request, now)


def held_card_ids(paths: FleetPaths, *, now: datetime | None = None) -> set[str]:
    """Return every card the builder path is actively holding right now.

    Read from the dispatch tree itself rather than from ``_ready_builders()``,
    deliberately: a node demoted out of the builder role, cordoned, or deleted
    from the registry while a worker is mid-flight still holds the card it
    claimed, and keying the answer on role would release that card to a local
    lane while a remote worker was running it.

    A record this cannot read counts as HELD. The release is the only direction
    that can put a lane and a builder on the same card, so an unreadable
    record must fail towards the behaviour that has no race. An unreadable
    dispatch TREE raises, so the caller can fall back to withholding
    everything rather than silently reporting an idle builder path.

    Args:
        paths: The fleet object tree carrying dispatch requests and statuses.
        now: The instant to judge offer leases against; defaults to now.

    Returns:
        The set of card ids the builder path holds.

    Raises:
        BuilderDispatchError: The dispatch tree could not be enumerated.
    """
    root = paths.root / "dispatch"
    stamp = now or _now()
    held: set[str] = set()
    try:
        entries = root.iterdir() if root.exists() else ()
        nodes = sorted(entry for entry in entries if entry.is_dir())
    except OSError as exc:
        raise BuilderDispatchError(f"dispatch tree is unreadable: {root}") from exc
    for node_dir in nodes:
        try:
            requests = sorted(node_dir.glob("*.json"))
        except OSError as exc:
            raise BuilderDispatchError(f"dispatch tree is unreadable: {node_dir}") from exc
        for path in requests:
            # The filename is the card id by construction (request_path), so a
            # request too malformed to name its own card still names one here.
            card_id = path.stem
            try:
                request = _load(path) or {}
                card_id = str(request.get("card_id") or card_id)
                status = _load(status_path(paths, node_dir.name, card_id)) or {}
            except BuilderDispatchError:
                held.add(card_id)
                continue
            if request_holds_card(request, status, stamp):
                held.add(card_id)
    return held


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
    normalized_labels = sorted(str(label).strip().lower() for label in labels)
    route = logical_route(labels)
    if route is None:
        raise BuilderDispatchError("card must select exactly one logical route")
    ready = _ready_builders(paths)
    selected_node = None
    for view in ready:
        existing = _load(request_path(paths, view.name, card_id))
        if not existing:
            continue
        same_binding = (
            existing.get("repository"),
            existing.get("base_ref"),
            existing.get("base_revision"),
            existing.get("labels"),
        ) == (repository, base_ref, revision, normalized_labels)
        if same_binding:
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
        prior = _load(status_path(paths, view.name, card_id)) or {}
        if (
            prior.get("request_id") == existing.get("request_id")
            and prior.get("state") == "running"
        ):
            return None
        selected_node = view.name
        break
    if selected_node is None:
        builders = [view for view in ready if _node_load(paths, view.name) < BUILDER_CAPACITY]
        decision = scheduler.select(builders, scheduler.Workload("job", card_id))
        if decision.node is None:
            return None
        selected_node = decision.node
    scheduler.place(
        paths,
        scheduler.Workload("job", card_id),
        writer=writer,
        views=[view for view in ready if view.name == selected_node],
    )
    stamp = now or _now()
    identity = json.dumps(
        [card_id, selected_node, repository, base_ref, revision, normalized_labels],
        separators=(",", ":"),
    )
    request = {
        "schema": "skfleet.builder-dispatch/v1",
        "request_id": hashlib.sha256(identity.encode()).hexdigest(),
        "card_id": card_id,
        "node": selected_node,
        "role": ROLE,
        "provider": PROVIDER,
        "logical_route": route,
        "repository": repository,
        "base_ref": base_ref,
        "base_revision": revision,
        "labels": normalized_labels,
        "offered_at": _iso(stamp),
        "lease_expires_at": _iso(stamp + timedelta(seconds=LEASE_SECONDS)),
        "writer": {"role": writer.role, "node": writer.node, "identity": writer.identity},
    }
    path = request_path(paths, selected_node, card_id)
    existing = _load(path)
    if existing and existing.get("request_id") == request["request_id"]:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(request, indent=2, sort_keys=True) + "\n")
    return request


def decline_reason(
    paths: FleetPaths,
    core: dict,
    labels: list[str] | tuple[str, ...],
) -> str | None:
    """Explain, without writing anything, why offer() would decline this card.

    The scheduler's offer() answers None for reasons an operator cannot see:
    a frozen plane, an empty or role-less node registry, and, most commonly,
    a request whose retries are already spent (MAX_ATTEMPTS reached), which
    parks a card forever under its current source binding. This mirrors the
    decline branches of offer() read-only so the rotation loop can log one
    line naming the reason; None means offer() would return a request.

    Args:
        paths: The fleet tree.
        core: The card core ({"id", "meta"}), as passed to offer().
        labels: The card's labels, as passed to offer().

    Returns:
        A short reason string, or None when the card is offerable.
    """
    if not store.actuation_allowed(paths):
        return "actuation-frozen"
    if not eligible(core, labels):
        return "ineligible"
    card_id = str(core["id"]).lower()
    if not valid_name(card_id):
        return "invalid-card-id"
    try:
        repository, base_ref, revision = _source(core)
    except BuilderDispatchError as exc:
        return f"invalid-source: {exc}"
    normalized_labels = sorted(str(label).strip().lower() for label in labels)
    ready = _ready_builders(paths)
    if not ready:
        return "no-ready-builder"
    for view in ready:
        existing = _load(request_path(paths, view.name, card_id))
        if not existing:
            continue
        prior = _load(status_path(paths, view.name, card_id)) or {}
        same_generation = prior.get("request_id") == existing.get("request_id")
        same_binding = (
            existing.get("repository"),
            existing.get("base_ref"),
            existing.get("base_revision"),
            existing.get("labels"),
        ) == (repository, base_ref, revision, normalized_labels)
        if same_binding:
            if (
                same_generation
                and prior.get("state") in TERMINAL_STATES
                and not (
                    prior.get("state") == "failed"
                    and int(prior.get("attempt") or 1) < MAX_ATTEMPTS
                )
            ):
                return (
                    f"terminal: node={view.name} state={prior.get('state')}"
                    f" attempt={prior.get('attempt')}"
                )
            return None
        if same_generation and prior.get("state") == "running":
            return f"superseded-binding-running: node={view.name}"
        return None
    builders = [view for view in ready if _node_load(paths, view.name) < BUILDER_CAPACITY]
    if not builders:
        # The common real cause, and the one that used to reach the log as
        # "unschedulable: unschedulable ()": every Ready builder is full, so
        # the scheduler was handed nothing to choose between. Name the loads.
        loads = ", ".join(
            f"{view.name}={_node_load(paths, view.name)}/{BUILDER_CAPACITY}" for view in ready
        )
        return f"builders-at-capacity: {loads}"
    decision = scheduler.select(builders, scheduler.Workload("job", card_id))
    if decision.node is None:
        return f"unschedulable: {decision.reason}"
    return None


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


def _guard_path() -> str:
    """Resolve the mediated write guard from configuration or this checkout."""
    configured = os.environ.get("SKFLEET_PI_CARDSTORE_GUARD")
    if configured:
        if not Path(configured).is_file():
            raise BuilderDispatchError("configured Pi CardStore write guard does not exist")
        return configured
    discovered = shutil.which("pi-cardstore-guard.mjs")
    if discovered:
        return discovered
    if _BUNDLED_GUARD.is_file():
        return str(_BUNDLED_GUARD)
    raise BuilderDispatchError("mediated worker runtime is not installed")


def worker_command(request: dict, owner: str, claim_revision: str, workspace: Path) -> list[str]:
    """Build the only permitted remote worker command."""
    worker = os.environ.get("SKFLEET_PI") or shutil.which("pi")
    if not worker:
        raise BuilderDispatchError("mediated worker runtime is not installed")
    guard = _guard_path()
    route = str(request.get("logical_route") or "")
    if route not in LOGICAL_ROUTES:
        raise BuilderDispatchError("dispatch request has no supported logical route")
    home = str(Path.home())
    sovereign_home = str(Path(SOVEREIGN_HOME).expanduser())
    return [
        "/usr/bin/env",
        "-i",
        f"HOME={home}",
        f"PATH={home}/.skenv/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG=C.UTF-8",
        f"SKAGENT={owner}",
        f"SKCAPSTONE_AGENT={owner}",
        f"SKCAPSTONE_HOME={sovereign_home}",
        f"SKFLEET_CARD_ID={request['card_id']}",
        f"SKFLEET_CLAIM_REVISION={claim_revision}",
        worker,
        "--no-approve",
        "--extension",
        guard,
        "--name",
        owner,
        "--provider",
        PROVIDER,
        "--model",
        route,
        "--thinking",
        "off",
        "--no-context-files",
        "--no-skills",
        "--tools",
        _WORKER_TOOLS,
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
    """Reconcile active builders and fill this node's bounded worker slots."""
    directory = paths.root / "dispatch" / node
    with _request_exclusion(directory / ".consumer"):
        return _consume_available(paths, coordination_home, node, launcher, materializer)


def _consume_available(
    paths: FleetPaths,
    coordination_home: Path,
    node: str,
    launcher: Callable[[list[str], Path], object] | None,
    materializer: Callable[[dict, Path], Path],
) -> dict | None:
    """Refresh every active generation before admitting pending requests."""
    spec = store.read_spec(paths, "node", node) or {}
    if spec.get("spec", {}).get("role") != ROLE or spec.get("spec", {}).get("actuate") is not True:
        return None
    if not store.actuation_allowed(paths):
        return None
    directory = paths.root / "dispatch" / node
    paths_to_visit = sorted(directory.glob("*.json")) if directory.exists() else ()
    result = None
    active = 0
    for path in paths_to_visit:
        with _request_exclusion(path):
            request = _load(path) or {}
            if (
                request.get("schema") != "skfleet.builder-dispatch/v1"
                or request.get("node") != node
            ):
                continue
            prior = _load(status_path(paths, node, request["card_id"])) or {}
            if prior.get("state") != "running":
                continue
            if prior.get("request_id") != request.get("request_id"):
                active += 1  # Preserve an uncertain prior generation and its claim.
                continue
            result = _reconcile_running(paths, coordination_home, node, request, prior)
            active += result["state"] == "running"
    for path in paths_to_visit:
        with _request_exclusion(path):
            request = _load(path) or {}
            if (
                request.get("schema") != "skfleet.builder-dispatch/v1"
                or request.get("node") != node
            ):
                continue
            prior = _load(status_path(paths, node, request["card_id"])) or {}
            if (
                prior
                and prior.get("request_id") != request.get("request_id")
                and (prior.get("owner") or prior.get("claim_revision"))
            ):
                if prior.get("state") == "running":
                    continue
                prior_owner = str(prior.get("owner") or "")
                prior_revision = str(prior.get("claim_revision") or "")
                released = bool(prior_owner and prior_revision) and _release_exact(
                    coordination_home,
                    request["card_id"],
                    prior_owner,
                    prior_revision,
                    actor=prior_owner,
                )
                result = _write_status(
                    paths,
                    node,
                    request,
                    "blocked",
                    owner=prior_owner,
                    claim_revision=prior_revision,
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=released,
                )
                continue
            if prior.get("request_id") == request.get("request_id"):
                if prior.get("state") == "running":
                    continue
                if prior.get("state") not in {"failed", "frozen"} or (
                    prior.get("state") == "failed"
                    and int(prior.get("attempt") or 1) >= MAX_ATTEMPTS
                ):
                    continue
            try:
                expires = datetime.strptime(
                    request["lease_expires_at"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                expires = datetime.min.replace(tzinfo=timezone.utc)
            if _now() > expires:
                result = _write_status(
                    paths,
                    node,
                    request,
                    "blocked",
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=False,
                    error="unclaimed offer expired",
                )
                continue
            if active >= BUILDER_CAPACITY:
                continue
            attempt = int(prior.get("attempt") or 0) + 1
            owner = f"pi-builder-standby-{node}-{request['card_id']}"
            workspace = paths.root / "workspaces" / owner
            if not store.actuation_allowed(paths):
                return None
            try:
                _ensure_request_matches_current_card(coordination_home, request)
            except BuilderDispatchError as exc:
                _write_status(
                    paths,
                    node,
                    request,
                    "blocked",
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=False,
                    error=str(exc),
                )
                continue
            try:
                materializer(request, workspace)
            except BuilderDispatchError as exc:
                _write_status(
                    paths,
                    node,
                    request,
                    "failed",
                    attempt=attempt,
                    retryable=attempt < MAX_ATTEMPTS,
                    claim_released=False,
                    error=str(exc),
                )
                continue
            if not store.actuation_allowed(paths):
                return _write_status(
                    paths,
                    node,
                    request,
                    "frozen",
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=False,
                )
            try:
                _ensure_request_matches_current_card(coordination_home, request)
            except BuilderDispatchError as exc:
                _write_status(
                    paths,
                    node,
                    request,
                    "blocked",
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=False,
                    error=str(exc),
                )
                continue
            try:
                assert_claim_permitted(coordination_home, request["card_id"], owner)
            except ClaimRefusedError as exc:
                _write_status(
                    paths,
                    node,
                    request,
                    "blocked",
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=False,
                    error=str(exc),
                )
                continue
            try:
                Board(coordination_home).claim_task(owner, request["card_id"])
            except TaskUnclaimable as exc:
                # ziowk01-wsl, 2026-09-18: card 59553966 was voided and replaced
                # while it sat in this queue, claim_task raised, and the bare
                # ValueError unwound sknoded's whole main loop. One unusable
                # card must cost one card, not the node worker. Only this typed
                # refusal is caught: a corrupt store, an unreadable card core or
                # a permissions failure still propagates and still kills the
                # unit, because those are not survivable per-card conditions.
                logger.warning(
                    "builder dispatch skipping card %s on %s: unclaimable (%s): %s",
                    request["card_id"],
                    node,
                    exc.reason,
                    exc,
                )
                if not exc.terminal:
                    # Ordinary contention (another owner, an unmet dependency)
                    # clears on its own. Writing a terminal status here would
                    # park the card forever, since offer() declines a request
                    # generation whose status is terminal and nothing rewrites
                    # an unchanged source binding. Leave the request untouched
                    # and let the next pass retry it, costing no attempt.
                    continue
                # Keep going: skipping ONE card must not stop this pass from
                # servicing the rest of the queue, which is the whole point.
                result = _write_status(
                    paths,
                    node,
                    request,
                    "blocked",
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=False,
                    error=f"unclaimable: {exc}",
                    unclaimable_reason=exc.reason,
                )
                continue
            card = CardStore(coordination_home).fold(request["card_id"])
            revision = str(card.meta.get("_claim_revision") or "") if card else ""
            if not card or card.owner != owner or not revision:
                raise BuilderDispatchError("claimed generation is not authoritative")
            try:
                _ensure_request_matches_current_card(coordination_home, request)
            except BuilderDispatchError as exc:
                released = _release_exact(
                    coordination_home, request["card_id"], owner, revision, actor=owner
                )
                _write_status(
                    paths,
                    node,
                    request,
                    "blocked",
                    owner=owner,
                    claim_revision=revision,
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=released,
                    error=str(exc),
                )
                continue
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
            result = _write_status(
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
            active += 1
    return result


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
