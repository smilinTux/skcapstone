"""Host-neutral governed workspace runtime bootstrap for eligible worker nodes.

Provides create/retire of isolated per-card worktrees, exact claim/unit
mapping, capacity admission from live memory/swap/active work/logical bucket
limits, and Herdr generation reclaim that touches only exact terminal
generations. Node placement stays an execution detail: no literal host or
model contract bindings live in this module.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_CARD_RE = re.compile(r"^[0-9a-f]{8}$")
_LANE_RE = re.compile(r"^(?:codex|glm|qwen|kimi|escalate)$")
_BUCKET_RE = re.compile(r"^(?:S|M|L|XL)$")
_UNIT_RE = re.compile(r"^skfleet-worker-(codex|glm|qwen|kimi|escalate)-([0-9a-f]{8})\.service$")

HERDR_TERMINAL_STATES = frozenset({"done"})
HERDR_PRESERVE_STATES = frozenset({"idle", "working", "blocked", "unknown"})
SCHEMA = "skfleet.workspace-runtime/v1"

Runner = Callable[..., subprocess.CompletedProcess[str]]


class WorkspaceRuntimeError(ValueError):
    """Governed workspace runtime policy was not satisfied."""


@dataclass(frozen=True)
class WorkspaceBinding:
    """Exact claim, unit, workspace, and generation for one worker seat."""

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
    """Capacity admission outcome for one logical bucket seat."""

    admitted: bool
    reason: str
    mem_available_kb: int | None = None
    swap_free_kb: int | None = None
    active_work: int | None = None
    bucket_capacity: int | None = None


@dataclass(frozen=True)
class RuntimeAdvertisement:
    """Node-local capability advertisement without host or model bindings."""

    schema: str
    workspaces_root: str
    buckets: dict[str, int]
    mem_reserve_kb: int
    swap_reserve_kb: int
    supports_herdr_reclaim: bool = True
    supports_isolated_worktrees: bool = True


def worker_unit_name(lane: str, card_id: str) -> str:
    """Return the exact governed unit for one lane and card id."""
    if not _LANE_RE.fullmatch(lane) or not _CARD_RE.fullmatch(card_id):
        raise WorkspaceRuntimeError("invalid worker unit identity")
    return f"skfleet-worker-{lane}-{card_id}.service"


def parse_worker_unit(unit: str) -> tuple[str, str]:
    """Parse one governed unit into lane and card id."""
    match = _UNIT_RE.fullmatch(str(unit or ""))
    if match is None:
        raise WorkspaceRuntimeError("unit does not match governed worker pattern")
    return match.group(1), match.group(2)


def herdr_generation(agent: Mapping[str, Any]) -> str:
    """Fingerprint one Herdr agent observation for exact-generation reclaim."""
    payload = {
        "name": str(agent.get("name") or agent.get("agent") or ""),
        "workspace_id": str(agent.get("workspace_id") or ""),
        "pane_id": str(agent.get("pane_id") or ""),
        "cwd": str(agent.get("cwd") or ""),
        "state_change_seq": agent.get("state_change_seq"),
        "revision": agent.get("revision"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


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


def _with_registry_lock(home: Path) -> Any:
    """Return an exclusive flock handle for the runtime registry."""
    lock = _lock_path(home)
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def advertise_runtime(
    home: Path,
    *,
    workspaces_root: Path,
    buckets: Mapping[str, int],
    mem_reserve_kb: int = 1_048_576,
    swap_reserve_kb: int = 262_144,
) -> RuntimeAdvertisement:
    """Return a host-neutral runtime advertisement for one node root."""
    if not Path(workspaces_root).is_absolute():
        raise WorkspaceRuntimeError("workspaces root must be absolute")
    normalized: dict[str, int] = {}
    for name, capacity in buckets.items():
        if not _BUCKET_RE.fullmatch(str(name)) or int(capacity) < 0:
            raise WorkspaceRuntimeError("logical bucket capacity is invalid")
        normalized[str(name)] = int(capacity)
    if mem_reserve_kb < 0 or swap_reserve_kb < 0:
        raise WorkspaceRuntimeError("capacity reserves must be non-negative")
    return RuntimeAdvertisement(
        schema=SCHEMA,
        workspaces_root=str(Path(workspaces_root).resolve()),
        buckets=normalized,
        mem_reserve_kb=int(mem_reserve_kb),
        swap_reserve_kb=int(swap_reserve_kb),
    )


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse Linux meminfo keys required for fail-closed admission."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        fields = rest.split()
        if not fields:
            continue
        try:
            values[key] = int(fields[0])
        except ValueError as exc:
            raise WorkspaceRuntimeError(f"meminfo field {key} is not an integer") from exc
    required = ("MemAvailable", "SwapTotal", "SwapFree")
    missing = [key for key in required if key not in values]
    if missing:
        raise WorkspaceRuntimeError("meminfo is missing required fields: " + ",".join(missing))
    return values


def admit_capacity(
    *,
    meminfo_text: str,
    active_work: int,
    bucket: str,
    bucket_capacity: int,
    mem_reserve_kb: int = 1_048_576,
    swap_reserve_kb: int = 262_144,
) -> AdmissionDecision:
    """Admit one seat only when live headroom and bucket capacity are safe."""
    if not _BUCKET_RE.fullmatch(bucket):
        return AdmissionDecision(False, "invalid-bucket")
    if active_work < 0 or bucket_capacity < 0:
        return AdmissionDecision(False, "invalid-capacity")
    try:
        meminfo = parse_meminfo(meminfo_text)
    except WorkspaceRuntimeError as exc:
        return AdmissionDecision(False, str(exc))
    mem_available = meminfo["MemAvailable"]
    swap_total = meminfo["SwapTotal"]
    swap_free = meminfo["SwapFree"]
    if mem_available < mem_reserve_kb:
        return AdmissionDecision(
            False,
            "unsafe-memory",
            mem_available_kb=mem_available,
            swap_free_kb=swap_free,
            active_work=active_work,
            bucket_capacity=bucket_capacity,
        )
    # Reason: swap must stay readable and leave configured free headroom when
    # present; missing swap (total 0) is allowed only when free is also 0.
    if swap_total < 0 or swap_free < 0 or swap_free > swap_total:
        return AdmissionDecision(False, "unsafe-swap")
    if swap_total > 0 and swap_free < swap_reserve_kb:
        return AdmissionDecision(
            False,
            "unsafe-swap",
            mem_available_kb=mem_available,
            swap_free_kb=swap_free,
            active_work=active_work,
            bucket_capacity=bucket_capacity,
        )
    if active_work >= bucket_capacity:
        return AdmissionDecision(
            False,
            "bucket-full",
            mem_available_kb=mem_available,
            swap_free_kb=swap_free,
            active_work=active_work,
            bucket_capacity=bucket_capacity,
        )
    return AdmissionDecision(
        True,
        "admitted",
        mem_available_kb=mem_available,
        swap_free_kb=swap_free,
        active_work=active_work,
        bucket_capacity=bucket_capacity,
    )


def _git(runner: Runner, checkout: Path, *args: str) -> str:
    result = runner(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "git failed").strip()[:160]
        raise WorkspaceRuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _assert_not_shared_checkout(repo: Path, target: Path, shared_roots: Sequence[Path]) -> None:
    """Refuse shared checkouts and escaped workspace paths."""
    resolved_repo = repo.resolve()
    resolved_target = target if target.exists() else target.parent.resolve() / target.name
    for root in shared_roots:
        shared = root.resolve()
        if resolved_repo == shared or resolved_target == shared:
            raise WorkspaceRuntimeError("shared-checkout collision refused")
        if shared in resolved_target.parents:
            raise WorkspaceRuntimeError("shared-checkout collision refused")


def create_isolated_workspace(
    home: Path,
    *,
    repo: Path,
    workspaces_root: Path,
    card_id: str,
    claim_revision: str,
    owner: str,
    lane: str,
    bucket: str,
    base_revision: str,
    shared_checkouts: Sequence[Path] = (),
    runner: Runner = subprocess.run,
) -> WorkspaceBinding:
    """Create one isolated per-card worktree and register its exact binding."""
    if not _CARD_RE.fullmatch(card_id):
        raise WorkspaceRuntimeError("card id must be 8 lowercase hex digits")
    if not claim_revision or not owner:
        raise WorkspaceRuntimeError("claim revision and owner are required")
    if not _BUCKET_RE.fullmatch(bucket):
        raise WorkspaceRuntimeError("logical bucket is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", base_revision):
        raise WorkspaceRuntimeError("base_revision must be 40 lowercase hex")
    if not workspaces_root.is_absolute():
        raise WorkspaceRuntimeError("workspaces root must be absolute")
    unit = worker_unit_name(lane, card_id)
    workspaces_root.mkdir(parents=True, exist_ok=True)
    target = workspaces_root / f"{card_id}-{claim_revision[:12]}"
    _assert_not_shared_checkout(repo, target, shared_checkouts)
    if target.exists() or target.is_symlink():
        raise WorkspaceRuntimeError("workspace path already exists")
    repo = repo.resolve()
    if not (repo / ".git").exists():
        raise WorkspaceRuntimeError("repository checkout is missing")
    toplevel = _git(runner, repo, "rev-parse", "--show-toplevel")
    if Path(toplevel).resolve() != repo:
        raise WorkspaceRuntimeError("repository path is not the git toplevel")
    if target.resolve() == repo:
        raise WorkspaceRuntimeError("shared-checkout collision refused")
    _git(runner, repo, "rev-parse", "--verify", f"{base_revision}^{{commit}}")
    # Reason: detached worktrees avoid branch-name collisions across successor
    # claim revisions for the same card id.
    _git(runner, repo, "worktree", "add", "--detach", str(target), base_revision)
    try:
        resolved = target.resolve(strict=True)
        root = workspaces_root.resolve()
        if resolved.parent != root:
            raise WorkspaceRuntimeError("workspace escaped workspaces root")
        if _git(runner, resolved, "rev-parse", "--show-toplevel") != str(resolved):
            raise WorkspaceRuntimeError("created workspace is not its git root")
        head = _git(runner, resolved, "rev-parse", "HEAD")
        if head != base_revision:
            raise WorkspaceRuntimeError("workspace HEAD does not match base_revision")
        binding = WorkspaceBinding(
            card_id=card_id,
            claim_revision=claim_revision,
            owner=owner,
            lane=lane,
            unit=unit,
            workspace=str(resolved),
            generation=f"{card_id}:{claim_revision}:{unit}",
            bucket=bucket,
            base_revision=base_revision,
        )
        lock = _with_registry_lock(home)
        try:
            registry = _load_registry(home)
            if registry["bindings"].get(card_id) is not None:
                raise WorkspaceRuntimeError("card already has an active workspace binding")
            for row in registry["bindings"].values():
                if row.get("unit") == unit:
                    raise WorkspaceRuntimeError("unit already bound to another card")
                if row.get("workspace") == str(resolved):
                    raise WorkspaceRuntimeError("workspace already bound")
            registry["bindings"][card_id] = asdict(binding)
            _write_registry(home, registry)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        return binding
    except Exception:
        # Reason: rollback the worktree when registry registration fails so a
        # successor can activate without colliding on the same path.
        try:
            _git(runner, repo, "worktree", "remove", "--force", str(target))
        except WorkspaceRuntimeError:
            pass
        raise


def get_binding(home: Path, card_id: str) -> WorkspaceBinding | None:
    """Return the active binding for one card, if present."""
    row = _load_registry(home)["bindings"].get(card_id)
    if row is None:
        return None
    return WorkspaceBinding(**row)


def retire_workspace(
    home: Path,
    binding: WorkspaceBinding,
    *,
    repo: Path,
    runner: Runner = subprocess.run,
) -> None:
    """Retire one exact registered binding and remove its worktree."""
    lock = _with_registry_lock(home)
    try:
        registry = _load_registry(home)
        current = registry["bindings"].get(binding.card_id)
        if current != asdict(binding):
            raise WorkspaceRuntimeError("binding does not match active registry generation")
        path = Path(binding.workspace)
        if path.exists():
            _git(runner, repo.resolve(), "worktree", "remove", "--force", str(path))
        del registry["bindings"][binding.card_id]
        _write_registry(home, registry)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def rollback_create(
    home: Path,
    binding: WorkspaceBinding,
    *,
    repo: Path,
    runner: Runner = subprocess.run,
) -> None:
    """Roll back a created binding after a later activation failure."""
    retire_workspace(home, binding, repo=repo, runner=runner)


def activate_successor(
    home: Path,
    *,
    previous: WorkspaceBinding | None,
    next_binding: WorkspaceBinding,
) -> None:
    """Refuse successor activation while a prior exact generation remains."""
    if previous is not None:
        if previous.card_id != next_binding.card_id:
            raise WorkspaceRuntimeError("successor card id mismatch")
        active = get_binding(home, previous.card_id)
        if active is not None and active.generation == previous.generation:
            raise WorkspaceRuntimeError("successor blocked by active prior generation")
    current = get_binding(home, next_binding.card_id)
    if current is None:
        raise WorkspaceRuntimeError("successor binding is not registered")
    if current != next_binding:
        raise WorkspaceRuntimeError("successor binding generation mismatch")


def select_herdr_reclaims(
    agents: Sequence[Mapping[str, Any]],
    recorded_generations: Mapping[str, str],
) -> list[dict[str, str]]:
    """Return only exact terminal Herdr generations eligible for reclaim.

    Idle, working, blocked, unknown, and changed generations are preserved.
    """
    selected: list[dict[str, str]] = []
    for agent in agents:
        if not isinstance(agent, Mapping):
            continue
        state = str(agent.get("agent_status") or agent.get("state") or "").strip().lower()
        name = str(agent.get("name") or agent.get("agent") or "")
        if not name:
            continue
        generation = herdr_generation(agent)
        recorded = recorded_generations.get(name)
        if state in HERDR_PRESERVE_STATES:
            continue
        if recorded is None:
            # Unknown recorded generation: fail closed by preserving.
            continue
        if generation != recorded:
            # Changed generation: preserve even if status looks terminal.
            continue
        if state not in HERDR_TERMINAL_STATES:
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


def exact_claim_unit_mapping(
    *,
    card_id: str,
    claim_revision: str,
    owner: str,
    lane: str,
    unit: str,
) -> bool:
    """Return True only when claim identity and unit map exactly."""
    if not claim_revision or not owner:
        return False
    try:
        expected = worker_unit_name(lane, card_id)
        parsed_lane, parsed_card = parse_worker_unit(unit)
    except WorkspaceRuntimeError:
        return False
    return unit == expected and parsed_lane == lane and parsed_card == card_id


def list_bindings(home: Path) -> list[WorkspaceBinding]:
    """Return all active bindings in stable card-id order."""
    registry = _load_registry(home)
    return [
        WorkspaceBinding(**registry["bindings"][card_id])
        for card_id in sorted(registry["bindings"])
    ]


def read_meminfo_file(path: Path = Path("/proc/meminfo")) -> str:
    """Read meminfo text, failing closed when unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceRuntimeError("meminfo is unreadable") from exc
