"""Governed remote card dispatch for builder standby nodes."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
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
from . import production_builder, scheduler, store
from .churn_breaker import ClaimRefusedError, assert_claim_permitted
from .node_controller import NodeView, node_views
from .paths import SOVEREIGN_HOME, FleetPaths, valid_name

ROLE = "builder-standby"
PROVIDER = "skgateway"
LOGICAL_ROUTES = frozenset({"sk-s", "sk-m", "sk-l", "sk-xl"})
LEASE_SECONDS = 900
TERMINAL_STATES = {"completed", "blocked", "failed", "stale", "awaiting-review"}
MAX_ATTEMPTS = 2
MATCH_RETRY_LIMIT = 3
BUILDER_CAPACITY = 4
CAPACITY_LABEL = "builder-capacity"
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


def _validated_status(path: Path, paths: FleetPaths, node: str) -> dict | None:
    """Return one canonical dispatch status, ignoring untrusted siblings."""
    if store._is_conflict_copy(path):
        return None
    status = _load(path) or {}
    card_id = status.get("card_id")
    if (
        status.get("schema") != "skfleet.builder-dispatch-status/v1"
        or status.get("node") != node
        or not isinstance(card_id, str)
        or not valid_name(card_id)
        or path != status_path(paths, node, card_id)
        or not isinstance(status.get("request_id"), str)
        or not status["request_id"]
    ):
        return None
    return status


def _dispatch_statuses(paths: FleetPaths, node: str) -> dict[str, dict]:
    """Return canonical validated dispatch statuses keyed by safe card id."""
    directory = status_path(paths, node, "placeholder").parent
    records: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        status = _validated_status(path, paths, node)
        if status is not None:
            records[status["card_id"]] = status
    return records


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
    excluded = {"host-pin"}
    if "SKFLEET_PRODUCTION_POLICY" not in os.environ:
        excluded.update({"codex-only", "qwen-only", "glm-only"})
    return (
        logical_route(labels) is not None
        and "source-only" in normalized
        and not any(label.startswith("seat-") for label in normalized)
        and not normalized.intersection(excluded)
        and isinstance(core.get("id"), str)
    )


def _source(core: dict) -> tuple[str, str, str]:
    """Read and validate exact credential-free source metadata."""
    raw_meta = core.get("meta")
    raw_links = core.get("links")
    meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
    links: dict = raw_links if isinstance(raw_links, dict) else {}

    def coalesce(field: str, *, lowercase: bool = False) -> str:
        raw_meta_value = meta.get(field)
        raw_link_value = links.get(field)
        if raw_meta_value is not None and not isinstance(raw_meta_value, str):
            raise BuilderDispatchError(f"{field} must be a string")
        if raw_link_value is not None and not isinstance(raw_link_value, str):
            raise BuilderDispatchError(f"{field} must be a string")
        meta_value = (raw_meta_value or "").strip()
        link_value = (raw_link_value or "").strip()
        if lowercase:
            meta_value = meta_value.lower()
            link_value = link_value.lower()
        if meta_value and link_value and meta_value != link_value:
            raise BuilderDispatchError(f"source binding conflict: {field}")
        return link_value or meta_value

    repository = coalesce("repository")
    base_ref = coalesce("base_ref")
    revision = coalesce("base_revision", lowercase=True)
    if not repository.startswith("https://") or "@" in repository:
        raise BuilderDispatchError("repository must be credential-free https")
    if not base_ref or len(base_ref) > 160 or any(ch.isspace() for ch in base_ref):
        raise BuilderDispatchError("base_ref is missing or unsafe")
    if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
        raise BuilderDispatchError("base_revision must be exact 40-hex")
    return repository, base_ref, revision


def _request_matches_current_card(
    coordination_home: Path, request: dict, *, retained_claim=False
) -> None:
    """Reject an offered request after any source or eligibility amendment."""
    card = CardStore(coordination_home).fold(str(request.get("card_id") or ""))
    if card is None:
        raise BuilderDispatchError("offered card is no longer foldable")
    core = {"id": card.id, "meta": card.meta, "links": card.links}
    labels = sorted(str(label).strip().lower() for label in card.labels)
    expected_labels = request.get("labels")
    if (
        not eligible(core, labels)
        or _source(core)
        != (request.get("repository"), request.get("base_ref"), request.get("base_revision"))
        or labels != expected_labels
    ):
        raise BuilderDispatchError("offered card changed after dispatch request")
    if request.get("production") is not None and not retained_claim:
        from .production_test_profile import contract, validate_profile

        try:
            validate_profile(
                request.get("test_profile"),
                contract(dict(core, acceptance_criteria=card.acceptance_criteria)),
                production_builder.policy(),
                environment=False,
            )
        except (ValueError, TypeError) as exc:
            raise BuilderDispatchError("offered test qualification changed") from exc


def _ensure_request_matches_current_card(
    coordination_home: Path, request: dict, *, retained_claim=False
) -> None:
    """Re-fold a request briefly before treating a mismatch as durable."""
    for check in range(MATCH_RETRY_LIMIT):
        try:
            _request_matches_current_card(
                coordination_home, request, retained_claim=retained_claim
            )
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


def _node_capacity(paths: FleetPaths, node: str) -> int:
    """Return one node's positive builder ceiling, defaulting to four."""
    spec = store.read_spec(paths, "node", node) or {}
    raw = (spec.get("labels") or {}).get(CAPACITY_LABEL)
    try:
        capacity = int(str(raw).strip())
    except (TypeError, ValueError):
        return BUILDER_CAPACITY
    return capacity if capacity > 0 else BUILDER_CAPACITY


def _node_load(paths: FleetPaths, node: str, *, production: bool = False) -> int:
    """Count occupied cards using the selected admission boundary."""
    active: set[str] = set()
    statuses = _dispatch_statuses(paths, node)
    directory = paths.root / "dispatch" / node
    for path in sorted(directory.glob("*.json")) if directory.exists() else ():
        request = _load(path) or {}
        card_id = str(request.get("card_id") or "")
        status = statuses.get(card_id, {})
        same = status.get("request_id") == request.get("request_id")
        terminal = TERMINAL_STATES | ({"awaiting-evidence"} if production else set())
        if same and status.get("state") in terminal:
            continue
        if production and not status and _lease_expired(request, _now()):
            continue
        active.add(card_id or path.stem)
    active.update(
        card_id for card_id, status in statuses.items() if status.get("state") == "running"
    )
    return len(active)


def _under_capacity(paths: FleetPaths, ready: list[NodeView]) -> list[NodeView]:
    """Return Ready builders below their own configured ceilings."""
    policy = production_builder.policy()
    if policy is not None:
        return _production_ready(paths, ready, policy, "resource-check")
    return [
        view for view in ready if _node_load(paths, view.name) < _node_capacity(paths, view.name)
    ]


def _production_ready(paths, ready, policy, card, *, exclude=None):
    """Reserve native queued and live quotas conservatively against fresh headroom."""
    from dataclasses import replace

    result = []
    for view in ready:
        statuses = _dispatch_statuses(paths, view.name)
        directory = paths.root / "dispatch" / view.name
        requests = {path.stem: _load(path) or {} for path in directory.glob("*.json")}
        cores, ram, unknown = 0.0, 0.0, False
        for candidate in set(requests) | set(statuses):
            if candidate == exclude:
                continue
            request, status = requests.get(candidate, {}), statuses.get(candidate, {})
            same = request.get("request_id") == status.get("request_id")
            if same and status.get("state") in TERMINAL_STATES | {"awaiting-evidence"}:
                continue
            if not status and _lease_expired(request, _now()):
                continue
            if not request and status.get("state") in TERMINAL_STATES | {"awaiting-evidence"}:
                continue
            resources = (status.get("production") or request.get("production") or {}).get(
                "resources"
            )
            if not isinstance(resources, dict):
                unknown = True
                break
            cpu, memory = resources.get("cpu_quota_percent"), resources.get("memory_max_bytes")
            if type(cpu) is not int or cpu <= 0 or type(memory) is not int or memory <= 0:
                unknown = True
                break
            cores += cpu / 100
            ram += memory / 1024**3
        if unknown:
            continue
        remaining = dict(view.allocatable)
        remaining["cores"] = float(remaining.get("cores", 0)) - cores
        remaining["ram_gb"] = float(remaining.get("ram_gb", 0)) - ram
        result.extend(
            production_builder.ready_nodes(
                paths, [replace(view, allocatable=remaining)], policy, card
            )
        )
    return result


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
    if request.get("schema") == "skfleet.builder-dispatch/v2":
        # Remote review expiry and process exit are never release evidence.
        return True
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
    # Missing request replication never turns destination review custody idle.
    for path in paths.status.glob("*/dispatch/*.json"):
        try:
            status = _validated_status(path, paths, path.parent.parent.name)
            if status and status.get("work_kind") == "review":
                held.add(path.stem)
        except (BuilderDispatchError, OSError, ValueError):
            held.add(path.stem)
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
    policy = production_builder.policy()
    if policy is not None:
        if (
            production_builder.socket.gethostname().split(".")[0].lower()
            != policy["authority_host"]
        ):
            raise BuilderDispatchError("only the production authority may offer work")
        # The authority is the sole offer writer. This local exclusion makes
        # reservation accounting plus publication atomic across its processes.
        with _request_exclusion(paths.root / "dispatch" / ".production-offer"):
            return _offer(paths, core, labels, writer=writer, now=now)
    return _offer(paths, core, labels, writer=writer, now=now)


def _offer(paths, core, labels, *, writer, now=None):
    """Publish within the production authority exclusion, or legacy admission."""
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
    production = production_builder.policy()
    test_profile = None
    if production is not None:
        from .production_test_profile import preflight

        try:
            test_profile = preflight(paths.root.parent, core, labels, production)
        except (OSError, ValueError) as exc:
            raise BuilderDispatchError("required-test-profile-unqualified") from exc
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
            prior = (
                _validated_status(status_path(paths, view.name, card_id), paths, view.name) or {}
            )
            if _unclaimed_expired_offer(existing, prior):
                if production is not None:
                    # A fresh generation must pass current host selection;
                    # the expired, unclaimed offer does not pin placement.
                    continue
                selected_node = view.name
                break
            if prior.get("request_id") == existing.get("request_id") and (
                prior.get("state") in TERMINAL_STATES
                and not (
                    prior.get("state") == "failed"
                    and int(prior.get("attempt") or 1) < MAX_ATTEMPTS
                )
            ):
                return None
            return existing
        prior = _validated_status(status_path(paths, view.name, card_id), paths, view.name) or {}
        if (
            prior.get("request_id") == existing.get("request_id")
            and prior.get("state") == "running"
        ):
            return None
        selected_node = view.name
        break
    if selected_node is None:
        builders = _under_capacity(paths, ready)
        if production is not None:
            # Spread already offered work before stale heartbeat headroom can
            # repeatedly select the same large machine. This is no count cap.
            builders = sorted(
                builders,
                key=lambda view: (_node_load(paths, view.name, production=True), view.name),
            )
            if not builders:
                return None
            selected_node = builders[0].name
        else:
            decision = scheduler.select(builders, scheduler.Workload("job", card_id))
            if decision.node is None:
                return None
            selected_node = decision.node
    binding = None
    if production is not None:
        binding = production_builder.node_binding(paths, selected_node, production)
        try:
            binding.update(
                production_builder.route_binding(production, card_id, route, normalized_labels)
            )
        except production_builder.production_routes.RouteUnavailableError:
            return None
    scheduler.place(
        paths,
        (
            production_builder.workload(card_id, binding)
            if binding
            else scheduler.Workload("job", card_id)
        ),
        writer=writer,
        views=[view for view in ready if view.name == selected_node],
    )
    stamp = now or _now()
    request = {
        "schema": "skfleet.builder-dispatch/v1",
        "request_id": secrets.token_hex(32),
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
    if binding is not None:
        request["production"] = binding
        request["test_profile"] = test_profile
    path = request_path(paths, selected_node, card_id)
    existing = _load(path)
    if existing and existing.get("request_id") == request["request_id"]:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(request, indent=2, sort_keys=True) + "\n")
    return request


def _unclaimed_expired_offer(request: dict, status: dict) -> bool:
    """Retry only a node-proven expiry that never acquired source custody."""
    return bool(
        status.get("request_id") == request.get("request_id")
        and status.get("state") == "blocked"
        and status.get("error") == "unclaimed offer expired"
        and status.get("attempt") == 0
        and not status.get("owner")
        and not status.get("claim_revision")
        and _lease_expired(request, _now())
    )


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
        prior = _validated_status(status_path(paths, view.name, card_id), paths, view.name) or {}
        same_generation = prior.get("request_id") == existing.get("request_id")
        same_binding = (
            existing.get("repository"),
            existing.get("base_ref"),
            existing.get("base_revision"),
            existing.get("labels"),
        ) == (repository, base_ref, revision, normalized_labels)
        if same_binding:
            if _unclaimed_expired_offer(existing, prior):
                return None
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
    builders = _under_capacity(paths, ready)
    production = production_builder.policy()
    if production is not None:
        if not builders:
            return "builders-resource-headroom-or-reservations"
        try:
            production_builder.route_binding(
                production, card_id, logical_route(labels), normalized_labels
            )
        except ValueError:
            return "gateway-card-route-unavailable"
        return None
    if not builders:
        # The common real cause, and the one that used to reach the log as
        # "unschedulable: unschedulable ()": every Ready builder is full, so
        # the scheduler was handed nothing to choose between. Name the loads.
        loads = ", ".join(
            f"{view.name}={_node_load(paths, view.name)}/{_node_capacity(paths, view.name)}"
            for view in ready
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
    if request.get("work_kind") == "review":
        payload["work_kind"] = "review"
    if request.get("production") is not None:
        payload["production"] = request["production"]
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
    if status.get("production") is not None:
        alive, code = production_builder.service_state(status, process)
        if alive is False:
            _PROCESSES.pop(request_id, None)
        return alive, code
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
    *,
    preserve_claim: bool = False,
) -> dict | None:
    """Release an exact prelaunch claim when the human freeze has won."""
    if store.actuation_allowed(paths):
        return None
    if preserve_claim:
        return {"state": "awaiting-evidence", "claim_released": False}
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
    _finalize_builder_admission(coordination_home, request, status)
    from . import builder_terminal

    if status.get("terminal_proof") == "qualified-terminal":
        try:
            builder_terminal.prove(coordination_home, status)
        except (OSError, ValueError, subprocess.SubprocessError):
            return status
        alive, exit_code = False, status.get("exit_code")
    elif (
        status.get("production") is not None
        and status.get("state") in {"awaiting-evidence", "awaiting-review"}
        and type(status.get("exit_code")) is int
    ):
        alive, exit_code = False, status["exit_code"]
    else:
        alive, exit_code = _process_state(status)
    if alive is None and status.get("production") is not None:
        try:
            if status.get("invocation") is None:
                status = builder_terminal.recover_collected(coordination_home, status)
            else:
                if builder_terminal.snapshot(status).get("LoadState") != "not-found":
                    raise ValueError("collected builder unit required")
                builder_terminal.prove(coordination_home, status, apply=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        else:
            status["terminal_proof"] = "qualified-terminal"
            alive, exit_code = False, None
    if (
        alive is False
        and status.get("production") is not None
        and status.get("terminal_proof") != "qualified-terminal"
    ):
        try:
            builder_terminal.observe(coordination_home, status)
        except (OSError, ValueError, subprocess.SubprocessError):
            # A missing receipt withholds collected-unit continuation only.
            pass
    owner = str(status.get("owner") or "")
    revision = str(status.get("claim_revision") or "")
    common = {
        "owner": owner,
        "claim_revision": revision,
        "pid": status.get("pid"),
        "pid_start_ticks": status.get("pid_start_ticks"),
        "attempt": int(status.get("attempt") or 1),
    }
    for key in ("admission_contract", "admission_terminal"):
        if key in status:
            common[key] = status[key]
    if status.get("terminal_proof") == "qualified-terminal":
        common["terminal_proof"] = "qualified-terminal"
    if status.get("continuation_consumed"):
        common["continuation_consumed"] = status["continuation_consumed"]
    if status.get("production") is not None:
        common.update(
            production=status["production"],
            unit=status.get("unit"),
            invocation=status.get("invocation"),
            route_preflight=status.get("route_preflight"),
        )
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
    elif status.get("production") is not None:
        from .builder_continue import original_outcome_pending
        from .production_exit import release_blocked
        from .source_bundle import SourceBundleError, publish_source

        if original_outcome_pending(coordination_home, request, status):
            return _write_status(
                paths,
                node,
                request,
                "awaiting-evidence",
                **common,
                exit_code=exit_code,
                claim_released=False,
                error="continued generation has not recorded a new outcome",
            )
        blocked = release_blocked(coordination_home, request, owner, revision)
        if blocked is not None:
            return _write_status(
                paths,
                node,
                request,
                "blocked",
                **common,
                exit_code=exit_code,
                claim_released=True,
                error=blocked["reason"],
                outcome_event=blocked["outcome_event"],
            )

        try:
            artifact = publish_source(
                coordination_home,
                request,
                owner,
                revision,
                paths.root / "workspaces" / owner,
                acknowledged=(status.get("source_artifact") or {}).get("manifest_sha256"),
            )
        except (SourceBundleError, OSError, ValueError, KeyError):
            # A process exit is not an outcome. Keep original custody for
            # review/recovery rather than replaying implementation work.
            return _write_status(
                paths,
                node,
                request,
                "awaiting-evidence",
                **common,
                exit_code=exit_code,
                claim_released=False,
                error="exact typed candidate artifacts pending",
            )
        return _write_status(
            paths,
            node,
            request,
            "awaiting-review",
            **common,
            exit_code=exit_code,
            claim_released=False,
            source_artifact=artifact,
        )
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


def _finalize_builder_admission(home: Path, request: dict, status: dict) -> None:
    """Discharge only a proven failed reservation, preserving workflow custody."""
    contract = status.get("admission_contract")
    if not isinstance(contract, dict) or status.get("admission_terminal"):
        return
    from . import production_admission as admission

    try:
        binding = {
            "card_id": request["card_id"],
            "owner": status["owner"],
            "claim_revision": status["claim_revision"],
            "request_id": request["request_id"],
            "attempt": status["attempt"],
        }
        unit = production_builder.unit_name(request, status["attempt"])
        if (
            status["request_id"] != request["request_id"]
            or status["card_id"] != request["card_id"]
            or status.get("production") != request.get("production")
            or status.get("unit") != unit
            or contract["binding"] != binding
        ):
            return
        observed = admission.unit_state(unit, terminal=True)
        if observed.get("ActiveState") != "failed":
            return
        invocation = observed.get("InvocationID", "")
        if status.get("invocation") not in (None, invocation):
            return
        policy = production_builder.policy()
        if policy is None:
            return
        proof = admission.finalize_failed_launch(
            home,
            policy,
            request["production"]["host"],
            unit,
            binding,
            contract["argv"],
            invocation=invocation,
            expected_card_revision=contract["card_revision"],
        )
        status["invocation"] = invocation
        status["admission_terminal"] = proof
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Missing or ambiguous custody remains charged; no release or retry.
        logger.warning(
            "failed builder admission requires exact custody: %s", request.get("card_id")
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
    production = request.get("production")
    model = production["model"] if production is not None else route
    prompt = (
        f"Work only SKCapstone card {request['card_id']}. "
        f"Claim revision {claim_revision}. Source is reconstructed at {workspace}. "
        "Use skcapstone coord and SKMail for all lifecycle updates."
    )
    if production is not None:
        from .production_brief import production_worker_brief

        prompt = production_worker_brief(
            card_id=request["card_id"],
            owner=owner,
            claim_revision=claim_revision,
            workspace=str(workspace),
            base_revision=request["base_revision"],
            title="Exact claimed native card",
            description=(
                "Read the exact current card through skcapstone coord show --json; "
                "its description and referenced TDD define the entire authorized task."
            ),
            acceptance_criteria=["Satisfy every exact current native card criterion."],
        )
        if request.get("_continuation"):
            transport_failure = request["_continuation"].get("binding", {}).get("transport")
            historical = (
                "The earlier session ended in a verified gateway transport failure, not a "
                "product verdict. Its transcript and all historical candidate outcomes "
                "remain preserved. Reuse the existing source; do not resume the oversized "
                "conversation or infer that a historical PASS approves this generation.\n\n"
                if transport_failure
                else "Original BLOCKED evidence remains historical.\n\n"
            )
            prompt = (
                "PRESERVED SOURCE CONTINUATION\n"
                + (
                    "Continue the exact retained source in this workspace. Read the current "
                    "card and implement only its remaining authorized work. Do not reset, "
                    if transport_failure
                    else "Continue the existing staged implementation in this workspace. "
                    "Do not reset, "
                )
                + "reimplement, release the claim or replace the source offer. Inspect the "
                "preserved changes and actual evidence; "
                + (
                    "finish remaining authorized implementation and validation, "
                    if transport_failure
                    else "finish only remaining validation, "
                )
                + "correct any inaccurate test chronology in the draft evidence, "
                "the already authorized commit and typed handoff. "
                "Machine Git identity is assigned in this environment.\n\n" + historical + prompt
            )
    from .worker_git import identity

    git_environment = identity(owner) if production is not None else {}
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
        *(f"{key}={value}" for key, value in git_environment.items()),
        worker,
        "--no-approve",
        "--extension",
        guard,
        "--name",
        owner,
        "--provider",
        PROVIDER,
        "--model",
        model,
        "--thinking",
        "off",
        "--no-context-files",
        "--no-skills",
        "--tools",
        _WORKER_TOOLS,
        "-p",
        prompt,
    ]


def materialize_source(request: dict, workspace: Path) -> Path:
    """Reconstruct and verify exact source without inheriting host Git settings."""
    # Match preseed custody literally; host insteadOf or Git environment must
    # not redirect the registered source or select another repository.
    git = ["/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if workspace.exists():
        head = subprocess.run(
            [*git, "rev-parse", "HEAD"],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
        )
        remote = subprocess.run(
            [*git, "remote", "get-url", "origin"],
            cwd=workspace,
            env=environment,
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
            [*git, "init", "--quiet", str(temporary)],
            [*git, "remote", "add", "origin", request["repository"]],
            [*git, "fetch", "--quiet", "--depth=1", "origin", request["base_revision"]],
            [*git, "checkout", "--quiet", "--detach", request["base_revision"]],
        )
        for command in commands:
            result = subprocess.run(
                command, cwd=temporary, env=environment, capture_output=True, text=True
            )
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
    from . import builder_continue, builder_retry

    spec = store.read_spec(paths, "node", node) or {}
    if spec.get("spec", {}).get("role") != ROLE or spec.get("spec", {}).get("actuate") is not True:
        return None
    if not store.actuation_allowed(paths):
        return None
    directory = paths.root / "dispatch" / node
    paths_to_visit = sorted(directory.glob("*.json")) if directory.exists() else ()
    requests = {
        str(request.get("card_id") or ""): request
        for path in paths_to_visit
        if (request := (_load(path) or {})).get("schema") == "skfleet.builder-dispatch/v1"
        and request.get("node") == node
    }
    result = None
    from . import review_dispatch

    for path in paths_to_visit:
        request = _load(path) or {}
        if request.get("schema") != review_dispatch.SCHEMA or request.get("node") != node:
            continue
        with _request_exclusion(path):
            try:
                result = review_dispatch.consume_review(
                    paths, coordination_home, node, request, launcher=launcher
                )
            except (OSError, ValueError, KeyError, TypeError):
                # Request/claim/intent stays held for exact evidence or recovery.
                continue
    active_cards: set[str] = set()
    reconciled_orphans: set[str] = set()
    for card_id, status in _dispatch_statuses(paths, node).items():
        if status.get("work_kind") == "review":
            active_cards.add(card_id)
            continue
        if status.get("state") != "running":
            continue
        request = requests.get(card_id) or {}
        if request.get("request_id") == status.get("request_id"):
            continue
        active_cards.add(card_id)  # Preserve live and unknown-liveness orphan generations.
        try:
            heartbeat = datetime.strptime(status["heartbeat_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (KeyError, TypeError, ValueError):
            continue
        if _now() <= heartbeat + timedelta(seconds=LEASE_SECONDS):
            continue
        generation = {
            "request_id": status["request_id"],
            "card_id": card_id,
            "node": node,
        }
        result = _reconcile_running(paths, coordination_home, node, generation, status)
        reconciled_orphans.add(card_id)
        if result["state"] != "running":
            active_cards.discard(card_id)
    for path in paths_to_visit:
        with _request_exclusion(path):
            request = _load(path) or {}
            if (
                request.get("schema") != "skfleet.builder-dispatch/v1"
                or request.get("node") != node
            ):
                continue
            prior = (
                _validated_status(status_path(paths, node, request["card_id"]), paths, node) or {}
            )
            if prior.get("state") not in {"running", "awaiting-evidence", "awaiting-review"}:
                continue
            if prior.get("request_id") != request.get("request_id"):
                active_cards.add(request["card_id"])
                continue
            try:
                continuing = builder_continue.attach(coordination_home, request, prior)
            except (ValueError, OSError):
                continue
            retry_handler = builder_continue if continuing else builder_retry
            if continuing or builder_retry.pending(request, prior):
                try:
                    retry_handler.check_attempt(paths, coordination_home, request, prior)
                except (ValueError, OSError):
                    if continuing:
                        continue  # Preserve changed continuation custody for the operator.
                else:
                    continue
            result = _reconcile_running(paths, coordination_home, node, request, prior)
            if result["state"] == "running":
                active_cards.add(request["card_id"])
            else:
                active_cards.discard(request["card_id"])
    for path in paths_to_visit:
        with _request_exclusion(path):
            request = _load(path) or {}
            if (
                request.get("schema") != "skfleet.builder-dispatch/v1"
                or request.get("node") != node
            ):
                continue
            prior = (
                _validated_status(status_path(paths, node, request["card_id"]), paths, node) or {}
            )
            try:
                continuing = builder_continue.attach(coordination_home, request, prior)
            except (ValueError, OSError):
                continue
            retry_handler = builder_continue if continuing else builder_retry
            retrying = continuing or builder_retry.pending(request, prior)
            if retrying:
                try:
                    retry_handler.check_attempt(paths, coordination_home, request, prior)
                except (ValueError, OSError):
                    continue
            if request["card_id"] in reconciled_orphans:
                continue
            if prior and prior.get("request_id") != request.get("request_id"):
                if prior.get("state") == "running" or (
                    prior.get("production") is not None
                    and prior.get("state") in {"awaiting-evidence", "awaiting-review"}
                ):
                    continue
                prior_owner = str(prior.get("owner") or "")
                prior_revision = str(prior.get("claim_revision") or "")
                if prior_owner and prior_revision and not prior.get("claim_released"):
                    released = _release_exact(
                        coordination_home,
                        request["card_id"],
                        prior_owner,
                        prior_revision,
                        actor=prior_owner,
                    )
                    if released:
                        result = _write_status(
                            paths,
                            node,
                            {
                                "request_id": prior["request_id"],
                                "card_id": request["card_id"],
                            },
                            str(prior.get("state") or "blocked"),
                            owner=prior_owner,
                            claim_revision=prior_revision,
                            attempt=int(prior.get("attempt") or 0),
                            claim_released=True,
                        )
                    continue
                prior = {}
            if prior.get("request_id") == request.get("request_id"):
                if prior.get("state") == "running":
                    continue
                if not retrying and (
                    prior.get("state") not in {"failed", "frozen"}
                    or (
                        prior.get("state") == "failed"
                        and int(prior.get("attempt") or 1) >= MAX_ATTEMPTS
                    )
                ):
                    continue
            try:
                expires = datetime.strptime(
                    (
                        request["_continuation"]["expires_at"]
                        if continuing
                        else request["lease_expires_at"]
                    ),
                    "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                expires = datetime.min.replace(tzinfo=timezone.utc)
            if _now() > expires:
                if retrying:
                    continue
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
            try:
                production = production_builder.validate_request(paths, node, request, local=True)
            except production_builder.production_routes.RouteUnavailableError:
                continue
            except ValueError:
                if retrying:
                    continue
                _write_status(
                    paths,
                    node,
                    request,
                    "blocked",
                    claim_released=False,
                    error="production request policy or host changed",
                )
                continue
            if production is not None:
                policy = production_builder.policy()
                if not any(
                    view.name == node
                    for view in _production_ready(
                        paths,
                        _ready_builders(paths),
                        policy,
                        request["card_id"],
                        exclude=request["card_id"],
                    )
                ):
                    continue
                try:
                    route_preflight = production_builder.production_routes.preflight(
                        policy, production
                    )
                except ValueError:
                    # Transient provider/catalog failure is retryable without
                    # a native claim or implementation attempt.
                    continue
            elif len(active_cards) >= _node_capacity(paths, node):
                continue
            attempt = int(prior.get("attempt") or 0) + 1
            owner = (
                f"pi-{production['family']}-builder-{node}-{request['card_id']}"
                if production
                else f"pi-builder-standby-{node}-{request['card_id']}"
            )
            workspace = paths.root / "workspaces" / owner
            if not store.actuation_allowed(paths):
                return None
            try:
                _ensure_request_matches_current_card(
                    coordination_home, request, retained_claim=retrying
                )
            except BuilderDispatchError as exc:
                if retrying:
                    continue
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
                if not continuing:
                    materializer(request, workspace)
            except BuilderDispatchError as exc:
                if retrying:
                    continue
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
                if retrying:
                    return None
                return _write_status(
                    paths,
                    node,
                    request,
                    "frozen",
                    attempt=int(prior.get("attempt") or 0),
                    claim_released=False,
                )
            try:
                _ensure_request_matches_current_card(
                    coordination_home, request, retained_claim=retrying
                )
            except BuilderDispatchError as exc:
                if retrying:
                    continue
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
                if retrying:
                    continue
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
                if not retrying:
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
                _ensure_request_matches_current_card(
                    coordination_home, request, retained_claim=retrying
                )
            except BuilderDispatchError as exc:
                if retrying:
                    continue
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
                preserve_claim=retrying,
            )
            if frozen is not None:
                return frozen
            startup_hello(coordination_home, owner, host=node)
            command = worker_command(request, owner, revision, workspace)
            if production is not None:
                try:
                    from .worker_git import preflight

                    preflight(command, workspace, owner)
                    production_builder.validate_request(paths, node, request, local=True)
                    command = production_builder.service_command(
                        request, attempt, command, workspace
                    )
                except ValueError as exc:
                    if retrying:
                        continue
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
                        claim_released=released,
                        error=f"production launch preflight failed: {exc}",
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
                preserve_claim=retrying,
            )
            if frozen is not None:
                return frozen
            run = launcher or (lambda argv, cwd: subprocess.Popen(argv, cwd=cwd))
            from .production_admission import AdmissionDeferredError

            exclusion_acquired = False
            admission_pending = False
            admission_reserved = False
            admission_contract = None
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
                        preserve_claim=retrying,
                    )
                    if frozen is not None:
                        return frozen
                    if production is not None:
                        from .production_admission import reserve_launch

                        # Unknown admission or spawn never grants claim release.
                        admission_pending = True
                        original_command = list(command)
                        admission_binding = {
                            "card_id": request["card_id"],
                            "owner": owner,
                            "claim_revision": revision,
                            "request_id": request["request_id"],
                            "attempt": attempt,
                        }
                        command = reserve_launch(
                            coordination_home,
                            policy,
                            production["host"],
                            production_builder.unit_name(request, attempt),
                            admission_binding,
                            command,
                        )
                        admission_reserved = True
                        if not retrying:
                            _write_status(
                                paths,
                                node,
                                request,
                                "running",
                                owner=owner,
                                claim_revision=revision,
                                attempt=attempt,
                                pid=None,
                                unit=production_builder.unit_name(request, attempt),
                                invocation=None,
                                route_preflight=route_preflight,
                            )

                    def spawn():
                        nonlocal admission_contract
                        if production is not None:
                            from ..seraph_review_cardstore import card_revision

                            card = CardStore(coordination_home).fold(request["card_id"])
                            if card is None:
                                raise BuilderDispatchError("launch card unavailable")
                            admission_contract = {
                                "binding": admission_binding,
                                "argv": original_command,
                                "card_revision": card_revision(card),
                            }
                            consumed = {}
                            if retrying:
                                key = (
                                    "continuation_consumed"
                                    if continuing
                                    else "operator_retry_consumed"
                                )
                                grant = (
                                    request["_continuation"]
                                    if continuing
                                    else request["operator_retry"]
                                )
                                consumed[key] = grant["id"]
                            _write_status(
                                paths,
                                node,
                                request,
                                "running",
                                owner=owner,
                                claim_revision=revision,
                                attempt=attempt,
                                pid=None,
                                unit=production_builder.unit_name(request, attempt),
                                invocation=None,
                                route_preflight=route_preflight,
                                admission_contract=admission_contract,
                                **consumed,
                            )
                        if production is not None:
                            from .production_admission import start_reserved

                            return start_reserved(
                                coordination_home,
                                production["host"],
                                command,
                                lambda argv: run(argv, workspace),
                            )
                        return run(command, workspace)

                    if retrying:
                        with retry_handler.consume(
                            paths,
                            coordination_home,
                            request,
                            prior,
                            route_preflight=route_preflight,
                        ):
                            process = spawn()
                    else:
                        process = spawn()
            except AdmissionDeferredError:
                if retrying or admission_reserved:
                    return None
                released = _release_exact(
                    coordination_home, request["card_id"], owner, revision, actor=owner
                )
                return _write_status(
                    paths,
                    node,
                    request,
                    "failed" if released else "blocked",
                    owner=owner,
                    claim_revision=revision,
                    claim_released=released,
                    attempt=int(prior.get("attempt") or 0),
                    retryable=released,
                    error="node-resource-capacity-deferred",
                )
            except Exception:
                if admission_pending:
                    logger.warning(
                        "production admission or launch requires custody: %s", request["card_id"]
                    )
                    return None
                if retrying:
                    # A spent authorization never releases custody or relaunches.
                    # Its persisted unknown unit state requires operator recovery.
                    logger.warning(
                        "operator retry refused or launch uncertain: %s", request["card_id"]
                    )
                    return None
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
            service = {}
            if production is not None:
                service = {
                    "unit": production_builder.unit_name(request, attempt),
                    "invocation": None,
                    "route_preflight": route_preflight,
                    "admission_contract": admission_contract,
                }
            if retrying:
                key = "continuation_consumed" if continuing else "operator_retry_consumed"
                grant = request["_continuation"] if continuing else request["operator_retry"]
                service[key] = grant["id"]
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
                **service,
            )
            active_cards.add(request["card_id"])
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
    status = _validated_status(status_path(paths, node, card_id), paths, node) or {}
    if request and status.get("request_id") != request.get("request_id"):
        return False
    if not request:
        request = {
            "request_id": status.get("request_id"),
            "card_id": status.get("card_id"),
            "node": node,
        }
    if not request.get("request_id") or request.get("card_id") != card_id:
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
