"""Governed workspace-runtime seam over fleet capacity (host-neutral).

Pure helpers decide admission and path plans. The bootstrap path
``create_isolated_workspace`` serializes under a registry lock, reads live
meminfo through an injectable reader, counts exact active bindings for the
logical bucket, and only then registers a binding. No git or subprocess.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from skcapstone.fleet.capacity import RESERVE_RAM_KB, RESERVE_SWAP_KB, admit_headroom

_CARD = re.compile(r"^[0-9a-f]{8}$")
_LANE = re.compile(r"^(?:codex|glm|qwen|kimi|escalate)$")
_BUCKET = re.compile(r"^(?:S|M|L|XL)$")
_UNIT = re.compile(r"^skfleet-worker-(codex|glm|qwen|kimi|escalate)-([0-9a-f]{8})\.service$")
_TERMINAL = frozenset({"done"})
_PRESERVE = frozenset({"idle", "working", "blocked", "unknown"})
SCHEMA = "skfleet.workspace-runtime/v1"

MeminfoReader = Callable[[], str]


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
    """Pure helper: compose capacity.admit_headroom with bucket occupancy."""
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


def _registry_dir(home: Path) -> Path:
    return Path(home) / "fleet" / "workspace-runtime"


def _registry_path(home: Path) -> Path:
    return _registry_dir(home) / "bindings.json"


def _lock_path(home: Path) -> Path:
    return _registry_dir(home) / "bindings.lock"


def _load_registry(home: Path) -> dict[str, Any]:
    path = _registry_path(home)
    if not path.exists():
        return {"schema": SCHEMA, "bindings": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceRuntimeError("workspace runtime registry is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != SCHEMA
        or not isinstance(payload.get("bindings"), dict)
    ):
        raise WorkspaceRuntimeError("workspace runtime registry is malformed")
    return payload


def _write_registry(home: Path, payload: Mapping[str, Any]) -> None:
    target = _registry_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=".bindings.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _with_registry_lock(home: Path):
    """Return an exclusive flock handle for the runtime registry."""
    lock = _lock_path(home)
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def list_bindings(home: Path) -> list[WorkspaceBinding]:
    """Return active bindings in stable card-id order."""
    registry = _load_registry(home)
    return [
        WorkspaceBinding(**registry["bindings"][card_id])
        for card_id in sorted(registry["bindings"])
    ]


def read_meminfo(path: Path = Path("/proc/meminfo")) -> str:
    """Read meminfo text; fail closed when unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceRuntimeError("meminfo is unreadable") from exc


def create_isolated_workspace(
    home: Path,
    *,
    advertisement: RuntimeAdvertisement,
    card_id: str,
    claim_revision: str,
    owner: str,
    lane: str,
    bucket: str,
    base_revision: str,
    shared_checkouts: Sequence[Path] = (),
    meminfo_path: Path | None = None,
    meminfo_reader: MeminfoReader | None = None,
) -> WorkspaceBinding:
    """Atomically admit and register one workspace binding under the registry lock.

    Counts exact active bindings for ``bucket``, reads live meminfo through the
    injectable reader/path, enforces advertised reserves and bucket capacity,
    then plans and registers. Callers supply actuators separately.
    """
    if bucket not in advertisement.buckets:
        raise WorkspaceRuntimeError("logical bucket is not advertised")
    if meminfo_reader is None:
        path = Path("/proc/meminfo") if meminfo_path is None else Path(meminfo_path)

        def meminfo_reader() -> str:
            return read_meminfo(path)

    lock = _with_registry_lock(home)
    try:
        registry = _load_registry(home)
        occupied = [WorkspaceBinding(**row) for row in registry["bindings"].values()]
        active_work = sum(1 for row in occupied if row.bucket == bucket)
        try:
            meminfo_text = meminfo_reader()
        except WorkspaceRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on any reader fault
            raise WorkspaceRuntimeError("meminfo is unreadable") from exc
        decision = admit_capacity(
            meminfo_text=meminfo_text,
            active_work=active_work,
            bucket=bucket,
            bucket_capacity=advertisement.buckets[bucket],
            mem_reserve_kb=advertisement.mem_reserve_kb,
            swap_reserve_kb=advertisement.swap_reserve_kb,
        )
        if not decision.admitted:
            raise WorkspaceRuntimeError(f"admission refused: {decision.reason}")
        binding = plan_isolated_workspace(
            workspaces_root=Path(advertisement.workspaces_root),
            card_id=card_id,
            claim_revision=claim_revision,
            owner=owner,
            lane=lane,
            bucket=bucket,
            base_revision=base_revision,
            shared_checkouts=shared_checkouts,
            occupied=occupied,
        )
        registry["bindings"][binding.card_id] = asdict(binding)
        _write_registry(home, registry)
        return binding
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def retire_workspace(home: Path, binding: WorkspaceBinding) -> None:
    """Retire one exact registered binding under the registry lock."""
    lock = _with_registry_lock(home)
    try:
        registry = _load_registry(home)
        current = registry["bindings"].get(binding.card_id)
        if current != asdict(binding):
            raise WorkspaceRuntimeError("binding does not match active registry generation")
        del registry["bindings"][binding.card_id]
        _write_registry(home, registry)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def retire_binding(
    binding: WorkspaceBinding, occupied: Sequence[WorkspaceBinding]
) -> list[WorkspaceBinding]:
    """Pure helper: remove one exact binding from an occupancy list."""
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
