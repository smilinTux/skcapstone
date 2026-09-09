"""Production adapters for the fail closed fleet worker liveness cycle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from skcapstone.card_store import CardStore
from skcapstone.fleet.paths import default_paths
from skcapstone.fleet.worker_liveness import (
    AssistanceRequest,
    CycleResult,
    LivenessObservation,
    RetirementReceipt,
    RuntimeActions,
    SQLiteReceiptJournal,
    WorkerProjection,
    run_cycle,
)

UNIT = re.compile(r"skfleet-worker-[a-z]+-([0-9a-f]{8})\.service")
SHA = re.compile(r"[0-9a-f]{40,64}")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded local command without a shell."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=10)


def _claim_revision(home: Path, card_id: str) -> str | None:
    """Read the authoritative current CardStore claim generation."""
    card = CardStore(home).fold(card_id)
    if card is None or card.status not in {"claimed", "doing", "ready"}:
        return None
    return str(getattr(card, "claim_revision", "") or card.meta.get("_claim_revision") or "")


def workspace_custody(path: Path) -> tuple[str, str, str] | None:
    """Return repository, head, and a digest of fresh Git custody state."""
    if not path.is_absolute() or not path.is_dir():
        return None
    values = []
    for args in (
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
    ):
        result = _run(args)
        if result.returncode:
            return None
        values.append(result.stdout.rstrip("\n"))
    repository, head, status = values
    if not repository or not SHA.fullmatch(head):
        return None
    payload = json.dumps(
        {"head": head, "path": str(path.resolve()), "repository": repository, "status": status},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return repository, head, hashlib.sha256(payload).hexdigest()


def _timestamp(value: object) -> datetime | None:
    """Parse one ISO or epoch timestamp, returning None on malformed input."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _cgroup_is_freshly_empty(cgroup: str | None, cgroup_root: Path) -> bool:
    """Read the exact host-owned membership file and require an empty cgroup."""
    try:
        relative = Path(cgroup or "").relative_to("/")
        process_file = (cgroup_root / relative / "cgroup.procs").resolve(strict=True)
        trusted_root = cgroup_root.resolve(strict=True)
        if not process_file.is_relative_to(trusted_root):
            return False
        members = tuple(int(value) for value in process_file.read_text(encoding="utf-8").split())
    except (OSError, RuntimeError, ValueError):
        return False
    return not members


def collect_observations(
    home: Path,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> tuple[LivenessObservation, ...]:
    """Collect conservative host local facts for every managed worker beat."""
    host = socket.gethostname()
    rows = []
    for beat_path in sorted((home / "fleet" / "beats").glob("*.json")):
        try:
            beat = json.loads(beat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        card_id = str(beat.get("card_id") or "")
        unit = str(beat.get("unit") or "")
        if not unit and re.fullmatch(r"[0-9a-f]{8}", card_id):
            listed = runner(
                [
                    "systemctl",
                    "--user",
                    "list-units",
                    "--all",
                    "--plain",
                    "--no-legend",
                    f"skfleet-worker-*-{card_id}.service",
                ]
            )
            candidates = [line.split()[0] for line in listed.stdout.splitlines() if line.split()]
            unit = candidates[0] if len(candidates) == 1 else ""
        if not UNIT.fullmatch(unit) or UNIT.fullmatch(unit).group(1) != card_id:
            continue
        show = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=ExecMainPID,ControlGroup,ActiveState,WorkingDirectory",
            ]
        )
        properties = (
            dict(line.split("=", 1) for line in show.stdout.splitlines() if "=" in line)
            if show.returncode == 0
            else {}
        )
        try:
            pid = int(properties.get("ExecMainPID", ""))
        except ValueError:
            pid = 0
        if pid <= 0:
            pid = 0
        active = properties.get("ActiveState") == "active"
        cgroup = (
            properties.get("ControlGroup")
            or (str(beat.get("cgroup") or "") if not active else "")
            or None
        )
        process_tree: tuple[int, ...] = ()
        if cgroup and cgroup.startswith("/"):
            try:
                current_processes = tuple(
                    int(value)
                    for value in (cgroup_root / cgroup.lstrip("/") / "cgroup.procs")
                    .read_text(encoding="utf-8")
                    .split()
                )
            except (OSError, ValueError):
                current_processes = None
            historical_processes = beat.get("process_tree")
            process_tree = (
                tuple(int(value) for value in historical_processes)
                if not active
                and isinstance(historical_processes, list)
                and all(isinstance(value, int) and value > 0 for value in historical_processes)
                else (current_processes or ())
            )
        else:
            current_processes = None
        workspace = Path(properties.get("WorkingDirectory") or ".")
        custody = workspace_custody(workspace)
        observed_at = datetime.now(timezone.utc)
        heartbeat_at = _timestamp(beat.get("beat_at"))
        terminal_at = _timestamp(beat.get("terminal_at"))
        rows.append(
            LivenessObservation(
                host=host,
                observer_host=host,
                observed_at=observed_at,
                owner=str(beat.get("agent") or beat.get("owner") or ""),
                card_id=card_id,
                claim_generation=str(beat.get("claim_revision") or ""),
                process_identity=f"pid:{pid}" if pid > 0 else None,
                session_id=str(beat.get("session") or beat.get("session_id") or "") or None,
                managed_session=True,
                process_alive=active,
                session_alive=properties.get("ActiveState") in {"active", "inactive"},
                live_children=(
                    max(0, len(current_processes) - (pid in current_processes))
                    if current_processes is not None
                    else 0
                ),
                child_activity_at=heartbeat_at,
                heartbeat_at=heartbeat_at,
                terminal_marker=beat.get("terminal_marker"),
                terminal_at=terminal_at,
                skmail_response_at=_timestamp(beat.get("skmail_response_at")),
                assistance_requested_at=_timestamp(beat.get("assistance_requested_at")),
                workspace_recoverable=custody is not None,
                workspace_custody="host-local-git" if custody else None,
                quiet_tool_wait=bool(beat.get("quiet_tool_wait", False)),
                interrupted=bool(beat.get("interrupted", False)),
                cleanup_failed=bool(beat.get("cleanup_failed", False)),
                claim_active=_claim_revision(home, card_id) is not None,
                current_claim_generation=_claim_revision(home, card_id),
                cgroup_processes=(
                    len(current_processes) if current_processes is not None else None
                ),
                unit=unit,
                pid=pid or None,
                process_tree=process_tree,
                cgroup=cgroup,
                process_observed_at=observed_at if properties else None,
                cgroup_observed_at=observed_at if cgroup else None,
                beat_id=str(
                    beat.get("beat_id") or hashlib.sha256(beat_path.read_bytes()).hexdigest()
                ),
                workspace_path=str(workspace.resolve()) if workspace.is_absolute() else None,
                workspace_repository=custody[0] if custody else None,
                workspace_head=custody[1] if custody else None,
                workspace_custody_at=observed_at if custody else None,
                workspace_custody_sha256=custody[2] if custody else None,
            )
        )
    return tuple(rows)


def authorize_observation(
    observation: LivenessObservation,
    *,
    home: Path,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
    hostname: str | None = None,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> bool:
    """Re-read every retirement fact from host and CardStore authority."""
    match = UNIT.fullmatch(observation.unit or "")
    if not match or match.group(1) != observation.card_id:
        return False
    local_host = hostname or socket.gethostname()
    if observation.host != local_host or observation.observer_host != local_host:
        return False
    if _claim_revision(home, observation.card_id) != observation.claim_generation:
        return False
    show = runner(
        [
            "systemctl",
            "--user",
            "show",
            observation.unit or "",
            "--property=Id,ExecMainPID,ControlGroup,ActiveState",
        ]
    )
    if show.returncode:
        return False
    properties = dict(line.split("=", 1) for line in show.stdout.splitlines() if "=" in line)
    if (
        properties.get("Id") != observation.unit
        or properties.get("ExecMainPID") != str(observation.pid)
        or properties.get("ActiveState") != "inactive"
        or properties.get("ControlGroup") not in {"", observation.cgroup}
        or set(properties) != {"Id", "ExecMainPID", "ControlGroup", "ActiveState"}
    ):
        return False
    if properties["ControlGroup"]:
        cgroup = runner(["systemctl", "--user", "status", observation.unit or ""])
        if cgroup.returncode not in {0, 3} or observation.cgroup not in cgroup.stdout:
            return False
    if observation.cgroup_processes != 0 or observation.live_children != 0:
        return False
    # This is the final host-owned retirement fence.  Do not authorize from
    # the membership cached during collection: a process can join the cgroup
    # after classification and before actuation.  The exact authoritative
    # file must still be readable and empty immediately before stop.
    if not _cgroup_is_freshly_empty(observation.cgroup, cgroup_root):
        return False
    custody = workspace_custody(Path(observation.workspace_path or ""))
    if custody != (
        observation.workspace_repository,
        observation.workspace_head,
        observation.workspace_custody_sha256,
    ):
        return False
    payload = None
    beat_digest = None
    for beat in sorted((home / "fleet" / "beats").glob("*.json")):
        try:
            raw = beat.read_bytes()
            candidate = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            candidate.get("card_id") == observation.card_id
            and candidate.get("claim_revision") == observation.claim_generation
        ):
            payload = candidate
            beat_digest = hashlib.sha256(raw).hexdigest()
            break
    if payload is None:
        return False
    return all(
        (
            (payload.get("beat_id") or beat_digest) == observation.beat_id,
            payload.get("host") == observation.host,
            payload.get("unit") == observation.unit,
            payload.get("pid") == observation.pid,
            payload.get("process_identity") == observation.process_identity,
            tuple(payload.get("process_tree") or ()) == observation.process_tree,
            payload.get("cgroup") == observation.cgroup,
            payload.get("card_id") == observation.card_id,
            payload.get("claim_revision") == observation.claim_generation,
            payload.get("terminal_marker") == observation.terminal_marker,
            payload.get("workspace_path") == observation.workspace_path,
            payload.get("workspace_repository") == observation.workspace_repository,
            payload.get("workspace_head") == observation.workspace_head,
            payload.get("workspace_custody_sha256") == observation.workspace_custody_sha256,
        )
    )


class ProductionActions:
    """Concrete adapters used by the timer driven fleet runtime."""

    def __init__(
        self,
        home: Path,
        agent: str,
        runner: Callable[[list[str]], object] = _run,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ):
        self.home = home
        self.agent = agent
        self.runner = runner
        self.cgroup_root = cgroup_root

    def request_assistance(self, request: AssistanceRequest) -> None:
        """Send a generation fenced structured SKMail status request."""
        script = Path(__file__).parents[3] / "scripts" / "fleet" / "skmail_work.py"
        self.runner(
            [
                os.environ.get("PYTHON", "python3"),
                str(script),
                "work.help.request",
                self.agent,
                request.owner,
                request.card_id,
                request.claim_generation,
                f"status requested; checkpoint due {request.checkpoint_due_at.isoformat()}",
            ]
        )

    def reconcile_projection(self, projection: WorkerProjection) -> None:
        """Publish the exact generation's liveness state through coord."""
        self.runner(
            [
                "skcapstone",
                "coord",
                "link",
                projection.card_id,
                "worker_liveness",
                f"{projection.owner}|{projection.claim_generation}|{projection.state}",
                "--agent",
                self.agent,
            ]
        )

    def publish_metrics(self, values: Mapping[str, float | int]) -> None:
        """Atomically publish fleet liveness metrics for scraping."""
        path = self.home / "evidence" / "fleet-liveness" / "metrics.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    def retire(self, receipt: RetirementReceipt) -> None:
        """Journal the authority fence, then retire only its exact unit."""
        SQLiteReceiptJournal(
            self.home / "evidence" / "fleet-liveness" / "retirements.sqlite3"
        ).append(receipt)
        if not _cgroup_is_freshly_empty(receipt.cgroup, self.cgroup_root):
            raise RuntimeError(f"retirement authority changed for {receipt.unit}")
        result = self.runner(["systemctl", "--user", "stop", receipt.unit])
        if getattr(result, "returncode", 1):
            raise RuntimeError(f"retirement failed for {receipt.unit}")


def run_production_cycle(
    home: Path | None = None,
    *,
    observations: Iterable[LivenessObservation] | None = None,
    projections: Iterable[WorkerProjection] | None = None,
    agent: str | None = None,
    actions_factory: Callable[[Path, str], ProductionActions] = ProductionActions,
    now: datetime | None = None,
) -> CycleResult:
    """Invoke the shared decision cycle through every production adapter."""
    root = home or default_paths().root.parent
    actor = agent or os.environ.get("SKAGENT", "skfleet-rotate")
    actions = actions_factory(root, actor)
    observed = tuple(observations) if observations is not None else collect_observations(root)
    projected = (
        tuple(projections)
        if projections is not None
        else tuple(
            WorkerProjection(row.owner, row.card_id, row.claim_generation, "active")
            for row in observed
        )
    )

    def authority(row: LivenessObservation) -> bool:
        """Revalidate one candidate against current local authority."""
        return authorize_observation(row, home=root)

    return run_cycle(
        observed,
        projected,
        now=now or datetime.now(timezone.utc),
        actions=RuntimeActions(
            actions.request_assistance,
            actions.reconcile_projection,
            actions.publish_metrics,
            actions.retire,
            authority,
        ),
    )
