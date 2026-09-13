"""Governed workspace-runtime seam over fleet capacity (host-neutral, pure).

Plans isolated workspace bindings, admits via capacity.admit_headroom, maps
claim/unit exactly, and selects only unchanged terminal Herdr generations.
Does not materialize worktrees; existing fleet actuators remain the writers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from skcapstone.fleet.capacity import RESERVE_RAM_KB, RESERVE_SWAP_KB, admit_headroom

_CARD = re.compile(r"^[0-9a-f]{8}$")
_LANE = re.compile(r"^(?:codex|glm|qwen|kimi|escalate)$")
_BUCKET = re.compile(r"^(?:S|M|L|XL)$")
_UNIT = re.compile(r"^skfleet-worker-(codex|glm|qwen|kimi|escalate)-([0-9a-f]{8})\.service$")
_TERMINAL = frozenset({"done"})
_PRESERVE = frozenset({"idle", "working", "blocked", "unknown"})
SCHEMA = "skfleet.workspace-runtime/v1"


class WorkspaceRuntimeError(ValueError):
    """Governed workspace runtime policy was not satisfied."""


@dataclass(frozen=True)
class WorkspaceBinding:
    """Exact claim, unit, path, and generation for one seat."""

    card_id: str
    claim_revision: str
    owner: str
    lane: str
    unit: str
    workspace: str
    generation: str
    bucket: str
    base_revision: str


@dataclass(frozen=True)
class AdmissionDecision:
    """Bucket admission outcome after capacity headroom checks."""

    admitted: bool
    reason: str
    mem_available_kb: int | None = None
    swap_free_kb: int | None = None
    active_work: int | None = None
    bucket_capacity: int | None = None


@dataclass(frozen=True)
class RuntimeAdvertisement:
    """Host-neutral node advertisement (no host/model bindings)."""

    schema: str
    workspaces_root: str
    buckets: dict[str, int]
    mem_reserve_kb: int
    swap_reserve_kb: int


def worker_unit_name(lane: str, card_id: str) -> str:
    """Return the exact governed unit for one lane and card id."""
    if not _LANE.fullmatch(lane) or not _CARD.fullmatch(card_id):
        raise WorkspaceRuntimeError("invalid worker unit identity")
    return f"skfleet-worker-{lane}-{card_id}.service"


def exact_claim_unit_mapping(
    *, card_id: str, claim_revision: str, owner: str, lane: str, unit: str
) -> bool:
    """True only when claim identity and unit map exactly."""
    if not claim_revision or not owner:
        return False
    try:
        expected = worker_unit_name(lane, card_id)
    except WorkspaceRuntimeError:
        return False
    match = _UNIT.fullmatch(unit)
    return bool(
        match and unit == expected and match.group(1) == lane and match.group(2) == card_id
    )


def advertise_runtime(
    *,
    workspaces_root: Path,
    buckets: Mapping[str, int],
    mem_reserve_kb: int = RESERVE_RAM_KB,
    swap_reserve_kb: int = RESERVE_SWAP_KB,
) -> RuntimeAdvertisement:
    """Advertise logical bucket capacity for one absolute workspaces root."""
    if not Path(workspaces_root).is_absolute() or mem_reserve_kb < 0 or swap_reserve_kb < 0:
        raise WorkspaceRuntimeError("invalid runtime advertisement")
    normalized = {}
    for name, capacity in buckets.items():
        if not _BUCKET.fullmatch(str(name)) or int(capacity) < 0:
            raise WorkspaceRuntimeError("logical bucket capacity is invalid")
        normalized[str(name)] = int(capacity)
    return RuntimeAdvertisement(
        SCHEMA,
        str(Path(workspaces_root).resolve()),
        normalized,
        int(mem_reserve_kb),
        int(swap_reserve_kb),
    )


def admit_capacity(
    *,
    meminfo_text: str,
    active_work: int,
    bucket: str,
    bucket_capacity: int,
    mem_reserve_kb: int = RESERVE_RAM_KB,
    swap_reserve_kb: int = RESERVE_SWAP_KB,
) -> AdmissionDecision:
    """Compose capacity.admit_headroom with logical bucket occupancy."""
    if not _BUCKET.fullmatch(bucket) or active_work < 0 or bucket_capacity < 0:
        return AdmissionDecision(False, "invalid-capacity")
    ok, reason, meminfo = admit_headroom(
        meminfo_text, mem_reserve_kb=mem_reserve_kb, swap_reserve_kb=swap_reserve_kb
    )
    mem_kb = None if meminfo is None else meminfo["MemAvailable"]
    swap_kb = None if meminfo is None else meminfo["SwapFree"]
    if not ok:
        return AdmissionDecision(False, reason, mem_kb, swap_kb, active_work, bucket_capacity)
    if active_work >= bucket_capacity:
        return AdmissionDecision(
            False, "bucket-full", mem_kb, swap_kb, active_work, bucket_capacity
        )
    return AdmissionDecision(True, "admitted", mem_kb, swap_kb, active_work, bucket_capacity)


def plan_isolated_workspace(
    *,
    workspaces_root: Path,
    card_id: str,
    claim_revision: str,
    owner: str,
    lane: str,
    bucket: str,
    base_revision: str,
    shared_checkouts: Sequence[Path] = (),
    occupied: Sequence[WorkspaceBinding] = (),
) -> WorkspaceBinding:
    """Plan an isolated path; refuse shared-checkout and occupancy collisions."""
    if (
        not _CARD.fullmatch(card_id)
        or not claim_revision
        or not owner
        or not _BUCKET.fullmatch(bucket)
        or not re.fullmatch(r"[0-9a-f]{40}", base_revision)
        or not workspaces_root.is_absolute()
    ):
        raise WorkspaceRuntimeError("invalid workspace plan")
    root = workspaces_root.resolve()
    for shared in shared_checkouts:
        shared_root = Path(shared).resolve()
        if root == shared_root or shared_root in root.parents or root in shared_root.parents:
            raise WorkspaceRuntimeError("shared-checkout collision refused")
    unit = worker_unit_name(lane, card_id)
    target = root / f"{card_id}-{claim_revision[:12]}"
    for row in occupied:
        if row.card_id == card_id:
            raise WorkspaceRuntimeError("card already has an active workspace binding")
        if row.unit == unit or Path(row.workspace).resolve() == target:
            raise WorkspaceRuntimeError("unit or workspace already bound")
    return WorkspaceBinding(
        card_id,
        claim_revision,
        owner,
        lane,
        unit,
        str(target),
        f"{card_id}:{claim_revision}:{unit}",
        bucket,
        base_revision,
    )


def retire_binding(
    binding: WorkspaceBinding, occupied: Sequence[WorkspaceBinding]
) -> list[WorkspaceBinding]:
    """Remove one exact binding; fail closed when the generation is absent."""
    remaining = [row for row in occupied if row != binding]
    if len(remaining) != len(occupied) - 1:
        raise WorkspaceRuntimeError("binding does not match active registry generation")
    return remaining


def activate_successor(
    *,
    previous: WorkspaceBinding | None,
    next_binding: WorkspaceBinding,
    occupied: Sequence[WorkspaceBinding],
) -> None:
    """Refuse successor activation while a prior exact generation remains."""
    if previous is not None:
        if previous.card_id != next_binding.card_id:
            raise WorkspaceRuntimeError("successor card id mismatch")
        if any(row.generation == previous.generation for row in occupied):
            raise WorkspaceRuntimeError("successor blocked by active prior generation")
    current = [row for row in occupied if row.card_id == next_binding.card_id]
    if len(current) != 1 or current[0] != next_binding:
        raise WorkspaceRuntimeError("successor binding generation mismatch")


def herdr_generation(agent: Mapping[str, Any]) -> str:
    """Fingerprint one Herdr observation for exact-generation reclaim."""
    payload = {
        "name": str(agent.get("name") or agent.get("agent") or ""),
        "workspace_id": str(agent.get("workspace_id") or ""),
        "pane_id": str(agent.get("pane_id") or ""),
        "cwd": str(agent.get("cwd") or ""),
        "state_change_seq": agent.get("state_change_seq"),
        "revision": agent.get("revision"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def select_herdr_reclaims(
    agents: Sequence[Mapping[str, Any]],
    recorded_generations: Mapping[str, str],
) -> list[dict[str, str]]:
    """Reclaim only unchanged terminal Herdr generations; preserve the rest."""
    selected: list[dict[str, str]] = []
    for agent in agents:
        if not isinstance(agent, Mapping):
            continue
        state = str(agent.get("agent_status") or agent.get("state") or "").strip().lower()
        name = str(agent.get("name") or agent.get("agent") or "")
        if not name or state in _PRESERVE:
            continue
        generation = herdr_generation(agent)
        if recorded_generations.get(name) != generation or state not in _TERMINAL:
            continue
        selected.append(
            {
                "name": name,
                "state": state,
                "generation": generation,
                "workspace_id": str(agent.get("workspace_id") or ""),
                "pane_id": str(agent.get("pane_id") or ""),
            }
        )
    return selected
