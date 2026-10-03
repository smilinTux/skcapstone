"""Validate explicit owner helper packets and call the fenced SKCoord API."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from skcoord.card_store import CardStore
from skcoord.coordination import Board, Task, TaskPriority

from .source_binding import source_binding_meta

MAX_PACKET_BYTES = 32768
PACKET_KEYS = frozenset(
    {"request_id", "title", "objective", "criteria", "allowed_paths", "verification_commands"}
)
HELPER_LIMIT = (
    "Work only in an isolated checkout at the inherited source revision. "
    "No shared source edits, push, deployment, live service change, provider call, "
    "Inbox processing, external action, or parent completion. Return the exact "
    "candidate or read-only findings and verification results to the parent owner."
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys instead of silently replacing packet fields."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate packet field: {key}")
        result[key] = value
    return result


def load_packet(path: Path) -> dict:
    """Read a bounded JSON packet without executing any supplied command."""
    with path.open("rb") as stream:
        payload = stream.read(MAX_PACKET_BYTES + 1)
    if len(payload) > MAX_PACKET_BYTES:
        raise ValueError(f"helper packet exceeds {MAX_PACKET_BYTES} bytes")
    try:
        packet = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("helper packet must be valid UTF-8 JSON") from exc
    return validate_packet(packet)


def _text(value: object, name: str, maximum: int) -> str:
    """Require a bounded, nonempty, printable string."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string of at most {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} must not contain control characters")
    return value.strip()


def _strings(value: object, name: str, maximum: int, *, empty: bool = False) -> list[str]:
    """Validate a small explicit list, rejecting duplicates and coercion."""
    if not isinstance(value, list) or len(value) > maximum or (not value and not empty):
        raise ValueError(f"{name} must contain {'0' if empty else '1'} to {maximum} strings")
    result = [_text(item, name, 1024) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def validate_packet(packet: object) -> dict:
    """Validate explicit, correlated helper work rather than inventing a split."""
    if not isinstance(packet, dict) or set(packet) != PACKET_KEYS:
        raise ValueError("helper packet requires exactly: " + ", ".join(sorted(PACKET_KEYS)))
    request_id = _text(packet["request_id"], "request_id", 64)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", request_id):
        raise ValueError("request_id must be a bounded identifier")
    title = _text(packet["title"], "title", 160)
    sizes = re.findall(r"\[(S|M|L|XL)\]", title)
    if len(sizes) != 1 or sizes[0] not in {"S", "M"}:
        raise ValueError("helper title must contain exactly one bounded size: [S] or [M]")
    if re.search(r"\[(?:REVIEW|REREVIEW|REPAIR|EPIC|SPRINT|HUMAN)\b", title, re.I):
        raise ValueError("helper title must not impersonate a governed review or container")
    paths = _strings(packet["allowed_paths"], "allowed_paths", 16, empty=True)
    for path in paths:
        parts = path.split("/")
        if (
            len(path) > 256
            or PurePosixPath(path).is_absolute()
            or any(part in {"", ".", "..", ".git"} for part in parts)
            or any(char in path for char in "\\*?[]:")
            or path.startswith("-")
        ):
            raise ValueError(
                "allowed_paths must be explicit relative paths without traversal or globs"
            )
    for index, path in enumerate(paths):
        if any(other.startswith(path + "/") for other in paths[index + 1 :]) or any(
            path.startswith(other + "/") for other in paths[index + 1 :]
        ):
            raise ValueError("allowed_paths must not contain overlapping paths")
    return {
        "request_id": request_id,
        "title": title,
        "objective": _text(packet["objective"], "objective", 4096),
        "criteria": _strings(packet["criteria"], "criteria", 8),
        "allowed_paths": paths,
        "verification_commands": _strings(
            packet["verification_commands"], "verification_commands", 8
        ),
    }


def _source(parent) -> dict[str, str]:
    """Freeze the parent's exact source, refusing conflicting projections."""
    values = {}
    for name in ("repository", "base_ref", "base_revision"):
        candidates = []
        for projection in (parent.meta, parent.links):
            value = projection.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"parent {name} must be a string")
            value = (value or "").strip()
            candidates.append(value.lower() if name == "base_revision" else value)
        if all(candidates) and candidates[0] != candidates[1]:
            raise ValueError(f"parent source binding conflict: {name}")
        values[name] = candidates[0] or candidates[1]
    return source_binding_meta(["source-only"], **values)


def build_helper(parent, packet: dict, actor: str, claim_revision: str) -> Task:
    """Build the same source-only Task for every exact request replay."""
    from skcoord.owner_helpers import parent_contract_digest

    packet = validate_packet(packet)
    if parent.owner != actor or parent.meta.get("claim_conflicts"):
        raise ValueError("only the unconflicted parent owner can request help")
    if (
        parent.status.value not in {"ready", "doing"}
        or parent.archived
        or parent.meta.get("voided")
    ):
        raise ValueError("parent must be an active, unarchived ready or doing card")
    if not claim_revision or parent.meta.get("_claim_revision") != claim_revision:
        raise ValueError("parent claim revision changed")
    if parent.meta.get("helper_parent_id"):
        raise ValueError(
            "helpers cannot recursively request helpers; return scope growth to the parent owner"
        )
    route = "sk-" + re.search(r"\[(S|M)\]", packet["title"]).group(1).lower()
    # Keep inherited restrictions and lineage; replace only route-shaped labels.
    labels = [
        label
        for label in parent.labels
        if not re.fullmatch(r"sk-[a-z]+(?:-[a-z]+)?", label.strip().lower())
    ]
    if any(label.lower() == "review" or label.lower().startswith("seat-") for label in labels):
        raise ValueError("governed seat or review parents cannot request source helpers")
    if re.search(r"\[(?:REVIEW|REREVIEW)\]", parent.title, re.I):
        raise ValueError("review parents must use their existing independent review contract")
    for label in ("source-only", "owner-helper", f"parent-{parent.id}", route):
        if label not in labels:
            labels.append(label)
    helper_id = hashlib.sha256(f"{parent.id}:{packet['request_id']}".encode()).hexdigest()[:8]
    source = _source(parent)
    description = (
        f"Bounded helper for {parent.id}, owned by {actor}, claim {claim_revision}.\n\n"
        f"Objective: {packet['objective']}\n"
        f"Allowed source paths: {json.dumps(packet['allowed_paths'])}. "
        "An empty list means read-only source assistance.\n"
        f"Verification commands: {json.dumps(packet['verification_commands'])}.\n\n"
        f"{HELPER_LIMIT}\n\nParent contract and restrictions (unchanged):\n"
        f"{parent.description}\nParent acceptance criteria:\n"
        + "\n".join(f"- {item}" for item in parent.acceptance_criteria)
    )
    return Task(
        id=helper_id,
        title=packet["title"],
        description=description,
        priority=TaskPriority(parent.priority),
        tags=labels,
        created_by=actor,
        created_at=parent.created_at,
        acceptance_criteria=[*packet["criteria"], HELPER_LIMIT],
        dependencies=list(parent.dependencies),
        meta={
            **source,
            "logical_route": route,
            "helper_request_id": packet["request_id"],
            "helper_parent_id": parent.id,
            "helper_parent_claim_revision": claim_revision,
            "helper_parent_contract_sha256": parent_contract_digest(parent),
            "helper_allowed_paths": packet["allowed_paths"],
            "helper_verification": packet["verification_commands"],
            "helper_objective": packet["objective"],
        },
    )


def request_help(
    home: Path,
    parent_id: str,
    actor: str,
    claim_revision: str,
    packet: dict,
    *,
    dry_run: bool = False,
    expected_parent: dict | None = None,
) -> dict:
    """Create one helper through the authoritative API; never claim or launch it."""
    board = Board(home)
    create_helper = getattr(board, "create_helper_task", None)
    if not callable(create_helper):
        raise ValueError(
            "installed SKCoord lacks Board.create_helper_task; use a reviewed compatible "
            "SKCoord release before requesting helpers (no fallback writes)"
        )
    from skcoord.card_store import explicit_creation_request_digest

    parent = CardStore(home).fold(parent_id)
    if parent is None:
        raise ValueError(f"parent card {parent_id} not found")
    task = build_helper(parent, packet, actor, claim_revision)
    if expected_parent is not None:
        from skcoord.owner_helpers import parent_contract_digest

        actual_parent = {
            "contract_sha256": parent_contract_digest(parent),
            "source": _source(parent),
            "labels": sorted(parent.labels),
            "dependencies": sorted(parent.dependencies),
        }
        if expected_parent != actual_parent:
            raise ValueError("parent differs from the original authorized helper mandate")
    digest = explicit_creation_request_digest(task.model_dump(mode="json"))
    if not dry_run:
        create_helper(task, digest, actor, parent_id, claim_revision)
    return {
        "parent_id": parent_id,
        "helper_id": task.id,
        "request_digest": digest,
        "actuation": "dry-run" if dry_run else "helper-created-or-replayed",
        "launch": "not-requested",
        "task": task.model_dump(mode="json"),
    }
