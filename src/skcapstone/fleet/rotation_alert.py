"""Consumed alerts for repeated fleet rotation failures and silent stalls."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from skcoord.itil import ITILManager

FAILURE_THRESHOLD = 2
STALL_THRESHOLD = 3
OBSERVATION_STALE_SECONDS = 20 * 60
_ALERT_SERVICE = "skfleet-rotate-alert.service"
_ALERT_DROP_IN = "skfleet-rotate-alert.conf"
_EXPECTED_RE = re.compile(r"\bDISPATCH_EXPECTED\|[^\n]*\bcount=(\d+)\b")
_ERROR_RE = re.compile(
    r"(?:traceback|exception|error:|attributeerror|keyerror|typeerror|failed)", re.IGNORECASE
)


@dataclass
class AlertState:
    """Host-local alert state. It must never live in the Syncthing tree."""

    consecutive_failures: int = 0
    consecutive_stalls: int = 0
    last_invocation: str = ""
    alert_kind: str = ""
    incident_id: str = ""
    last_detail: str = ""
    observed_at: int = 0
    delivered_to: list[str] = field(default_factory=list)
    observation_known: bool = True

    @classmethod
    def load(cls, path: Path) -> "AlertState":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                consecutive_failures=max(0, int(payload.get("consecutive_failures", 0))),
                consecutive_stalls=max(0, int(payload.get("consecutive_stalls", 0))),
                last_invocation=str(payload.get("last_invocation", "")),
                alert_kind=str(payload.get("alert_kind", "")),
                incident_id=str(payload.get("incident_id", "")),
                last_detail=str(payload.get("last_detail", "")),
                observed_at=max(0, int(payload.get("observed_at", 0))),
                delivered_to=[str(value) for value in payload.get("delivered_to", [])],
                observation_known=bool(payload.get("observation_known", False)),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":")) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def install_units(source_dir: Path, unit_dir: Path) -> tuple[Path, Path]:
    """Install the alert unit and rotation drop-in without reloading systemd."""
    service_source = source_dir / _ALERT_SERVICE
    drop_in_source = source_dir / _ALERT_DROP_IN
    if not service_source.is_file() or not drop_in_source.is_file():
        raise FileNotFoundError("rotation alert systemd assets are incomplete")
    unit_dir.mkdir(parents=True, exist_ok=True)
    drop_in_dir = unit_dir / "skfleet-rotate.service.d"
    drop_in_dir.mkdir(parents=True, exist_ok=True)
    service_target = unit_dir / _ALERT_SERVICE
    drop_in_target = drop_in_dir / "alert.conf"
    for source, target in (
        (service_source, service_target),
        (drop_in_source, drop_in_target),
    ):
        temporary = target.with_name(f".{target.name}.tmp")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    return service_target, drop_in_target


def extract_error(journal: str) -> str:
    """Return bounded error context rather than a generic failed message."""
    lines = [line.strip() for line in journal.splitlines() if line.strip()]
    traceback_indexes = [
        index for index, line in enumerate(lines) if line.lower().startswith("traceback")
    ]
    if traceback_indexes:
        selected = lines[traceback_indexes[-1] :]
    else:
        error_indexes = [index for index, line in enumerate(lines) if _ERROR_RE.search(line)]
        start = max(0, error_indexes[-1] - 2) if error_indexes else max(0, len(lines) - 4)
        selected = lines[start:]
    return " | ".join(selected[-10:])[-1800:] or "rotation exited nonzero without output"


def is_silent_stall(journal: str) -> bool:
    """True when selection expected a dispatch but the cycle launched nothing."""
    matches = _EXPECTED_RE.findall(journal)
    expected = int(matches[-1]) if matches else 0
    return expected > 0 and "LAUNCHED|" not in journal and "WOULD_LAUNCH|" not in journal


def _publish_observation(observation_root: Path, host: str, state: AlertState) -> None:
    """Publish one single-writer host snapshot for the authority to consume."""
    observation = AlertState(
        consecutive_failures=state.consecutive_failures,
        consecutive_stalls=state.consecutive_stalls,
        last_invocation=state.last_invocation,
        last_detail=state.last_detail,
        observed_at=state.observed_at,
        observation_known=state.observation_known,
    )
    observation.save(observation_root / f"{host}.json")


def _aggregate_observations(
    observation_root: Path, *, now: int, stale_after: int = OBSERVATION_STALE_SECONDS
) -> tuple[str, str, int, list[str], list[str], list[str]]:
    observations = {
        path.stem: AlertState.load(path) for path in sorted(observation_root.glob("*.json"))
    }
    stale_hosts = sorted(
        host
        for host, state in observations.items()
        if not state.observed_at or now - state.observed_at > stale_after
    )
    unknown_hosts = sorted(
        host
        for host, state in observations.items()
        if host not in stale_hosts and not state.observation_known
    )
    unavailable_hosts = sorted(set(stale_hosts) | set(unknown_hosts))
    fresh = {host: state for host, state in observations.items() if host not in unavailable_hosts}
    failures = {
        host: state
        for host, state in fresh.items()
        if state.consecutive_failures >= FAILURE_THRESHOLD
    }
    if failures:
        details = "; ".join(
            f"{host}: {state.last_detail}" for host, state in sorted(failures.items())
        )
        return (
            "failure",
            details[-1800:],
            max(state.consecutive_failures for state in failures.values()),
            unavailable_hosts,
            sorted(failures),
            [],
        )
    stalls = {
        host: state for host, state in fresh.items() if state.consecutive_stalls >= STALL_THRESHOLD
    }
    if stalls:
        hosts = ", ".join(sorted(stalls))
        return (
            "silent-stall",
            f"dispatch was expected but no worker launched on: {hosts}",
            max(state.consecutive_stalls for state in stalls.values()),
            unavailable_hosts,
            sorted(stalls),
            [],
        )
    pending_hosts = sorted(
        host
        for host, state in fresh.items()
        if state.consecutive_failures or state.consecutive_stalls
    )
    return "", "", 0, unavailable_hosts, [], pending_hosts


def read_journal(
    unit: str,
    invocation: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    command = ["journalctl", "--user", "-u", unit, "--no-pager", "-o", "cat"]
    if invocation:
        command.extend(["_SYSTEMD_INVOCATION_ID=" + invocation])
    else:
        command.extend(["-n", "120"])
    try:
        result = runner(command, capture_output=True, text=True, timeout=20, check=False)
    except Exception as exc:  # noqa: BLE001 - this is a best-effort failure observer
        return f"journal read failed: {type(exc).__name__}: {exc}"
    return result.stdout or result.stderr or ""


def _send_mail(
    subject: str,
    body: str,
    recipients: Sequence[str],
    *,
    sender: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> set[str]:
    delivered: set[str] = set()
    for recipient in recipients:
        try:
            result = runner(
                ["skmail", "send", sender, recipient, "urgent", subject, body],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:  # noqa: BLE001 - undelivered recipients are retried next cycle
            continue
        if result.returncode == 0:
            delivered.add(recipient)
    return delivered


def _open_incident(manager: ITILManager, host: str, kind: str, detail: str) -> str:
    incident = manager.create_incident(
        title=f"Fleet rotation {kind} on {host}: {detail[:120]}",
        severity="sev2",
        source="service_health",
        affected_services=["skfleet-rotate"],
        impact="Fleet dispatch cannot reliably replace completed workers.",
        managed_by="jarvis",
        created_by="skfleet-rotate-alert",
        tags=["fleet", "dispatch", "automatic"],
        failure_class="rotation-dispatch",
    )
    return incident.id


def _resolve_incident(manager: ITILManager, incident_id: str, detail: str) -> None:
    if not incident_id:
        return
    manager.update_incident(
        incident_id,
        agent="skfleet-rotate-alert",
        new_status="resolved",
        note=detail,
        resolution_summary=detail,
    )


def observe(
    outcome: str,
    journal: str,
    *,
    invocation: str,
    state_path: Path,
    shared_root: Path,
    observation_root: Path | None = None,
    host: str | None = None,
    authority_host: str = "chiap08",
    recipients: Sequence[str] = ("jarvis", "lumina"),
    sender: str = "jarvis",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    manager_factory: Callable[[Path], ITILManager] = ITILManager,
    clock: Callable[[], float] = time.time,
) -> str:
    """Fold one rotation outcome and deliver a threshold alert on the authority."""
    if outcome not in {"failure", "success"}:
        raise ValueError(f"unsupported rotation outcome: {outcome}")
    host = host or platform.node().split(".", 1)[0]
    state_resolved = state_path.expanduser().resolve()
    shared_resolved = shared_root.expanduser().resolve()
    if state_resolved == shared_resolved or shared_resolved in state_resolved.parents:
        raise ValueError("rotation alert counter state must be host-local, outside shared_root")
    observation_root = observation_root or (
        shared_root / "coordination/fleet/rotation-alert-observations"
    )
    state = AlertState.load(state_path)
    if invocation and invocation == state.last_invocation:
        return "duplicate"
    state.last_invocation = invocation

    if outcome == "failure":
        state.observation_known = True
        state.consecutive_failures += 1
        state.consecutive_stalls = 0
        kind = "failure"
        count = state.consecutive_failures
        threshold = FAILURE_THRESHOLD
        detail = extract_error(journal)
    else:
        marker_present = bool(_EXPECTED_RE.search(journal))
        state.observation_known = marker_present
        if marker_present:
            state.consecutive_failures = 0
        if not marker_present:
            detail = "successful rotation journal omitted DISPATCH_EXPECTED"
            kind = ""
            count = max(state.consecutive_failures, state.consecutive_stalls)
            threshold = STALL_THRESHOLD
        elif is_silent_stall(journal):
            state.consecutive_stalls += 1
            kind = "silent-stall"
            count = state.consecutive_stalls
            threshold = STALL_THRESHOLD
            detail = "ready cards persisted but the rotation launched no worker"
        else:
            state.consecutive_stalls = 0
            kind = "silent-stall"
            count = 0
            threshold = STALL_THRESHOLD
            detail = "rotation completed with no dispatch expected"
    local_count = count
    state.last_detail = detail
    state.observed_at = int(clock())
    state.save(state_path)
    _publish_observation(observation_root, host, state)

    authority = host == authority_host
    stale_hosts: list[str] = []
    pending_hosts: list[str] = []
    affected_hosts = [host]
    if authority:
        (
            kind,
            detail,
            count,
            stale_hosts,
            affected_hosts,
            pending_hosts,
        ) = _aggregate_observations(observation_root, now=state.observed_at)
        threshold = FAILURE_THRESHOLD if kind == "failure" else STALL_THRESHOLD
    recovered = (
        authority and bool(state.alert_kind) and not kind and not stale_hosts and not pending_hosts
    )
    if recovered:
        if authority:
            manager = manager_factory(shared_root)
            _resolve_incident(manager, state.incident_id, f"Rotation recovered on {host}.")
            _send_mail(
                f"RECOVERED-SKFLEET-ROTATION-{host}",
                f"skfleet-rotate recovered on {host}. The prior {state.alert_kind} state cleared.",
                recipients,
                sender=sender,
                runner=runner,
            )
        state.alert_kind = ""
        state.incident_id = ""
        state.delivered_to = []

    should_alert = bool(kind) and count >= threshold and state.alert_kind != kind
    if should_alert and authority:
        manager = manager_factory(shared_root)
        affected = ",".join(affected_hosts)
        if state.incident_id:
            manager.update_incident(
                state.incident_id,
                agent="skfleet-rotate-alert",
                note=f"Rotation condition changed to {kind} on {affected}: {detail}",
            )
        else:
            state.incident_id = _open_incident(manager, affected, kind, detail)
        state.alert_kind = kind
        state.delivered_to = []

    needs_delivery = (
        authority
        and bool(kind)
        and count >= threshold
        and (set(state.delivered_to) != set(recipients))
    )
    if needs_delivery:
        body = (
            f"skfleet-rotate detected {kind} on {','.join(affected_hosts)} after "
            f"{count} consecutive cycles.\n\n"
            f"Captured detail:\n{detail}\n\n"
            f"ITIL incident: {state.incident_id}\n"
            "Check systemctl --user status skfleet-rotate.service and its journal."
        )
        pending = [recipient for recipient in recipients if recipient not in state.delivered_to]
        state.delivered_to = sorted(
            set(state.delivered_to)
            | _send_mail(
                f"AUTO-ALERT-SKFLEET-ROTATION-{kind.upper()}-{host}",
                body,
                pending,
                sender=sender,
                runner=runner,
            )
        )

    state.save(state_path)
    if needs_delivery:
        if set(state.delivered_to) != set(recipients):
            return "delivery-pending"
        return "alerted"
    if should_alert and authority:
        return "alerted"
    if authority and stale_hosts:
        return "observation-stale"
    if authority and pending_hosts:
        return "pending"
    if should_alert:
        return "suppressed-non-authority"
    if recovered:
        return "recovered"
    return "pending" if local_count else "healthy"
