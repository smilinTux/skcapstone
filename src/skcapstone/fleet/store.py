"""Fleet object store: spec, status, freeze, and the ownership guard.

Single-writer-per-file is the load-bearing invariant (spec 3.2). This
module is the only code allowed to touch fleet files, and it enforces
ownership at write time: operator role writes spec, sknoded writes only
its own node's status subtree, scheduler (Phase 2) writes placements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..atomic_io import atomic_write_text
from .paths import FleetPaths, valid_name


class OwnershipError(Exception):
    """A writer attempted a write outside its ownership boundary."""


@dataclass(frozen=True)
class Writer:
    """Identity of a fleet writer (the seat, spec section 8).

    Attributes:
        role: One of operator, scheduler, sknoded, controller.
        node: Node name the writing process runs on.
        identity: capauth identity string; empty until signing (Card 3.5).
    """

    role: str
    node: str
    identity: str
    agent_seat: bool = False
    # True for the autonomous AI operator seat. The operator uses the `operator`
    # role for the ops channel (writing object specs), but it must NEVER write the
    # plane control files (the freeze kill-switch, the carve-out manifest). Those
    # stay human-only so the human keeps the one card that overrides the AI.


def writer_identity() -> str:
    """Resolve this process's capauth identity, or "" when unavailable."""
    try:
        from capauth import resolve_agent_identity

        return resolve_agent_identity().capauth_uri
    except Exception:
        return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _writer_block(writer: Writer) -> dict:
    return {
        "role": writer.role,
        "node": writer.node,
        "identity": writer.identity,
        "signature": None,
    }


def _maybe_sign(payload: dict, signer) -> dict:
    """Fill writer.signature via the given or default signer (Card 3.5)."""
    from .signing import canonical_bytes, default_signer

    sign = signer if signer is not None else default_signer()
    if sign is not None:
        try:
            payload["writer"]["signature"] = sign(canonical_bytes(payload))
        except Exception:
            payload["writer"]["signature"] = None
    return payload


#: Spec fields the autonomous AI operator seat may never change. It can register
#: and refresh an object, but these fields are the human's ratification lever
#: (same principle as freeze and plane files). An agent_seat writer's incoming
#: value must match what is already on disk (an empty list == absent for a new
#: object, so the AI may create a registration with none ratified).
_HUMAN_ONLY_SPEC_FIELDS: dict[str, tuple[str, ...]] = {
    "operatorapp": ("ratifiedStandardActions",),
}


def _guard_human_only_fields(
    kind: str, spec: dict, existing: dict, writer: "Writer"
) -> None:
    """Reject an AI-seat write that would change a human-only spec field."""
    if not writer.agent_seat:
        return
    prev_spec = existing.get("spec", {}) if existing else {}
    for field in _HUMAN_ONLY_SPEC_FIELDS.get(kind, ()):
        # Treat an empty list and an absent field as equal, so the AI may create
        # a fresh registration (nothing ratified) but never add or drop entries.
        new_val = spec.get(field) or None
        old_val = prev_spec.get(field) or None
        if new_val != old_val:
            raise OwnershipError(
                f"field {field!r} on {kind} is human-only; the autonomous AI seat "
                "cannot change it (a human ratifies standard actions)"
            )


def write_spec(
    paths: FleetPaths,
    kind: str,
    name: str,
    spec: dict,
    *,
    writer: Writer,
    labels: dict | None = None,
    signer=None,
) -> dict:
    """Write desired state for one object, bumping its generation.

    Only the operator seat may write spec (spec 3.2 ownership table).

    Returns:
        The full payload as written.
    Raises:
        OwnershipError: wrong role, or unsafe kind/name.
    """
    if writer.role != "operator":
        raise OwnershipError(f"role {writer.role!r} may not write spec files")
    if not (valid_name(kind) and valid_name(name)):
        raise OwnershipError(f"invalid kind/name: {kind!r}/{name!r}")
    path = paths.spec_path(kind, name)
    existing = _load(path) or {}
    _guard_human_only_fields(kind, spec, existing, writer)
    payload = {
        "kind": kind.capitalize(),
        "name": name,
        "labels": labels if labels is not None else existing.get("labels", {}),
        "generation": int(existing.get("generation", 0)) + 1,
        "spec": spec,
        "writer": _writer_block(writer),
        "updatedAt": _now_iso(),
    }
    payload = _maybe_sign(payload, signer)
    _dump(path, payload)
    return payload


def read_spec(paths: FleetPaths, kind: str, name: str) -> dict | None:
    """Read one spec file, or None when absent."""
    return _load(paths.spec_path(kind, name))


def list_specs(paths: FleetPaths, kind: str) -> list[dict]:
    """All specs of a kind, sorted by name. Zero objects cost nothing."""
    kind_dir = paths.objects / kind
    if not kind_dir.exists():
        return []
    out = []
    for p in sorted(kind_dir.glob("*.json")):
        payload = _load(p)
        if payload is not None:
            out.append(payload)
    return out


_NODE_FILES = {"heartbeat.json", "node.json", "join.json"}


def _changed(existing: dict | None, payload: dict) -> bool:
    if existing is None:
        return True
    strip = lambda d: {k: v for k, v in d.items() if k != "updatedAt"}  # noqa: E731
    return strip(existing) != strip(payload)


def write_status(
    paths: FleetPaths,
    kind: str,
    name: str,
    *,
    node: str,
    status: dict,
    conditions: list[dict],
    observed_generation: int,
    writer: Writer,
) -> bool:
    """Write observed state for one object on one node (write-on-change).

    Returns:
        True when a write happened, False when content was unchanged.
    Raises:
        OwnershipError: wrong role, or writer.node != node.
    """
    if writer.role != "sknoded":
        raise OwnershipError(f"role {writer.role!r} may not write status files")
    if writer.node != node:
        raise OwnershipError(f"{writer.node!r} may not write status for {node!r}")
    if not (valid_name(kind) and valid_name(name)):
        raise OwnershipError(f"invalid kind/name: {kind!r}/{name!r}")
    payload = {
        "kind": kind.capitalize(),
        "name": name,
        "node": node,
        "observedGeneration": observed_generation,
        "status": status,
        "conditions": conditions,
    }
    path = paths.status_path(node, kind, name)
    existing = _load(path)
    if not _changed(existing, payload):
        return False
    payload["updatedAt"] = _now_iso()
    _dump(path, payload)
    return True


def read_status(paths: FleetPaths, kind: str, name: str, node: str) -> dict | None:
    """Read one node's status file for an object, or None."""
    return _load(paths.status_path(node, kind, name))


def write_node_file(
    paths: FleetPaths,
    writer: Writer,
    filename: str,
    payload: dict,
    *,
    if_changed: bool = True,
) -> bool:
    """Write one of the node-owned singleton files (heartbeat/node/join).

    Only sknoded may call this, and only into its own subtree.
    """
    if writer.role != "sknoded":
        raise OwnershipError(f"role {writer.role!r} may not write node files")
    if filename not in _NODE_FILES:
        raise OwnershipError(f"not a node-owned file: {filename!r}")
    path = paths.node_status_dir(writer.node) / filename
    if if_changed and not _changed(_load(path), payload):
        return False
    body = dict(payload)
    body["updatedAt"] = _now_iso()
    _dump(path, body)
    return True


def read_node_file(paths: FleetPaths, node: str, filename: str) -> dict | None:
    """Read a node-owned singleton file, or None."""
    return _load(paths.node_status_dir(node) / filename)


def merged(paths: FleetPaths, kind: str, name: str) -> dict | None:
    """Assemble the object a reader sees: spec + placement + statuses.

    Each status gains a "stale" flag when its observedGeneration is behind
    the spec generation (spec 3.2: staleness detectable, never silent).
    """
    spec = read_spec(paths, kind, name)
    if spec is None:
        return None
    placement = _load(paths.placement_path(kind, name))
    statuses: list[dict] = []
    if paths.status.exists():
        for node_dir in sorted(p for p in paths.status.iterdir() if p.is_dir()):
            st = _load(node_dir / kind / f"{name}.json")
            if st is not None:
                st["stale"] = int(st.get("observedGeneration", 0)) < int(spec["generation"])
                statuses.append(st)
    return {"spec": spec, "placement": placement, "statuses": statuses}


def is_frozen(paths: FleetPaths) -> bool:
    """True when the fleet-wide kill-switch is on.

    An unreadable freeze file counts as frozen: when in doubt, halt
    actuation (running services are never touched by the flag itself).
    """
    path = paths.freeze_path()
    if not path.exists():
        return False
    payload = _load(path)
    if payload is None:
        return True
    return bool(payload.get("frozen"))


def set_frozen(paths: FleetPaths, frozen: bool, *, writer: Writer, reason: str = "") -> dict:
    """Toggle the kill-switch. HUMAN operator only (spec section 8).

    The autonomous AI operator seat (writer.agent_seat) can never toggle freeze:
    the kill switch is the human's sole override and the AI must not be able to
    unfreeze itself.
    """
    if writer.role != "operator" or writer.agent_seat:
        raise OwnershipError(
            "only a human operator may toggle freeze; the autonomous AI seat "
            "cannot touch its own kill switch"
        )
    payload = {
        "frozen": bool(frozen),
        "reason": reason,
        "writer": _writer_block(writer),
        "updatedAt": _now_iso(),
    }
    _dump(paths.freeze_path(), payload)
    return payload


def write_plane_file(paths: FleetPaths, name: str, payload: dict, *, writer: Writer) -> dict:
    """Write a plane control file (objects/_<name>.json: the carve-out manifest,
    and any future human-owned plane flag). HUMAN-only, same rule as the freeze.

    The autonomous AI operator seat can never write a plane file, so it cannot
    edit the carve-out manifest that lists its own guardrails. Names must start
    with an underscore (plane files are the `objects/_*` namespace).
    """
    if writer.role != "operator" or writer.agent_seat:
        raise OwnershipError(
            "plane files are human-only; the autonomous AI seat cannot write them"
        )
    if not name.startswith("_"):
        raise OwnershipError(f"plane files live in the objects/_* namespace: {name!r}")
    full = dict(payload)
    full["writer"] = _writer_block(writer)
    full["updatedAt"] = _now_iso()
    _dump(paths.freeze_path().parent / f"{name}.json", full)
    return full


def actuation_allowed(paths: FleetPaths) -> bool:
    """The one guard every actuating component checks before acting."""
    return not is_frozen(paths)


def write_placement(
    paths: FleetPaths,
    kind: str,
    name: str,
    *,
    node: str,
    reason: str,
    writer: Writer,
    signer=None,
) -> tuple[dict, bool]:
    """Write the scheduler's decision for one object (write-on-change).

    Only the scheduler role may write placements (spec 3.2 ownership table);
    it never writes status and never touches spec. placementGeneration bumps
    only when the decision content (node or reason) changed, so an idempotent
    re-run produces zero churn (R2).

    Returns:
        (payload as on disk, True when a write happened).
    Raises:
        OwnershipError: wrong role, or unsafe kind/name.
    """
    if writer.role != "scheduler":
        raise OwnershipError(f"role {writer.role!r} may not write placements")
    if not (valid_name(kind) and valid_name(name)):
        raise OwnershipError(f"invalid kind/name: {kind!r}/{name!r}")
    path = paths.placement_path(kind, name)
    existing = _load(path)
    if existing is not None and existing.get("node") == node and existing.get("reason") == reason:
        return existing, False
    payload = {
        "kind": kind.capitalize(),
        "name": name,
        "node": node,
        "reason": reason,
        "placementGeneration": int((existing or {}).get("placementGeneration", 0)) + 1,
        "writer": _writer_block(writer),
        "updatedAt": _now_iso(),
    }
    payload = _maybe_sign(payload, signer)
    _dump(path, payload)
    return payload, True


def read_placement(paths: FleetPaths, kind: str, name: str) -> dict | None:
    """Read one placement record, or None when absent."""
    return _load(paths.placement_path(kind, name))


def list_placements(paths: FleetPaths, kind: str | None = None) -> list[dict]:
    """All placement records, sorted by (kind, name). Zero objects cost nothing."""
    if not paths.placements.exists():
        return []
    kinds = (
        [kind]
        if kind is not None
        else sorted(p.name for p in paths.placements.iterdir() if p.is_dir())
    )
    out: list[dict] = []
    for k in kinds:
        kind_dir = paths.placements / k
        if not kind_dir.exists():
            continue
        for p in sorted(kind_dir.glob("*.json")):
            payload = _load(p)
            if payload is not None:
                out.append(payload)
    return out
