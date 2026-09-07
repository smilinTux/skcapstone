"""Disabled-by-default authoritative GLM admission consumer.

The executable surface in this module intentionally accepts no arguments.  All
execution authority, hosts, cards, commands, and paths are fixed reviewed
constants.  It is inert unless the fixed enable marker exists, and an active
GLM hold is still an unconditional denial in :mod:`glm_admission`.
"""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Sequence

from skcapstone.card_store import CardStore

from .glm_admission import (
    AUTHORITY_HOST,
    WORKER_HOSTS,
    AdmissionDenied,
    AdmissionSnapshot,
    Hold,
    HostReport,
    QueueSample,
    WorkerBinding,
    _abort_wave,
    _admit_wave,
)

ENABLED_MARKER = Path("/etc/skcapstone/glm-admission-enabled")
COORDINATION_HOME = Path("/home/skuser01/.skcapstone")
SNAPSHOT_HELPER = "/usr/local/libexec/skcapstone-glm-snapshot"
HOLD_HELPER = "/usr/local/libexec/skcapstone-glm-hold-snapshot"
MODEL = "zai/glm-4.6"
WORKER_CARDS = {
    "chiap01": ("b75f0cd8", "d127647f", "81b6b118"),
    "chiap02": ("177cd342", "e31e1577", "3600dd33"),
    "chiap03": ("62243d92", "efde62f9", "86a127b0"),
}


@dataclass(frozen=True)
class _Worker:
    """One fixed card and its derived execution custody."""

    host: str
    card_id: str
    agent_id: str
    session_id: str
    claim_id: str
    worktree: str

    def binding(self) -> WorkerBinding:
        """Return the reservation binding for this worker."""

        return WorkerBinding(
            host=self.host,
            card_id=self.card_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            claim_id=self.claim_id,
            workspace=self.worktree,
        )


@dataclass(frozen=True)
class _RollbackFailure:
    """One sanitized failure from a rollback compensator."""

    action: str
    card_id: str | None
    error_type: str


class _RollbackFailed(AdmissionDenied):
    """Raised after every compensator runs and one or more fail."""

    def __init__(self, failures: Sequence[_RollbackFailure]) -> None:
        self.failures = tuple(failures)
        detail = "; ".join(
            f"{failure.action}:{failure.card_id or 'wave'}:{failure.error_type}"
            for failure in self.failures
        )
        super().__init__(f"rollback failed: {detail}")


def _deny(reason: str) -> NoReturn:
    """Raise a stable fail-closed denial."""

    raise AdmissionDenied(reason)


def _run(
    command: Sequence[str], *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one fixed command without a shell, environment secrets, or provider IO."""

    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(COORDINATION_HOME.parent),
        "SKCAPSTONE_HOME": str(COORDINATION_HOME),
    }
    try:
        return subprocess.run(
            tuple(command),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        _deny("supported command failed")


def _json_command(command: Sequence[str]) -> dict[str, object]:
    """Run a fixed read-only command and require one JSON object."""

    result = _run(command)
    if result.returncode != 0:
        _deny("authoritative snapshot command failed")
    try:
        value = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError):
        _deny("malformed authoritative snapshot")
    if not isinstance(value, dict):
        _deny("malformed authoritative snapshot")
    return value


def _queue_sample(value: object) -> QueueSample:
    """Parse one exact queue sample from a helper response."""

    if not isinstance(value, dict) or set(value) != {"observed_at", "active", "queued"}:
        _deny("malformed queue sample")
    try:
        return QueueSample(**value)
    except TypeError:
        _deny("malformed queue sample")


def _host_snapshot(host: str) -> HostReport:
    """Collect one authoritative read-only snapshot from a fixed worker host."""

    value = _json_command(("ssh", "-oBatchMode=yes", host, SNAPSHOT_HELPER, "--json"))
    if set(value) != {
        "host",
        "reachable",
        "glm_auto_sessions",
        "observed_at",
        "http_429",
        "queue_samples",
    }:
        _deny("malformed host snapshot")
    samples = value["queue_samples"]
    if not isinstance(samples, list) or len(samples) != 2:
        _deny("malformed host queue samples")
    try:
        return HostReport(
            host=value["host"],
            reachable=value["reachable"],
            glm_auto_sessions=value["glm_auto_sessions"],
            observed_at=value["observed_at"],
            http_429=value["http_429"],
            queue_samples=(_queue_sample(samples[0]), _queue_sample(samples[1])),
        )
    except TypeError:
        _deny("malformed host snapshot")


def _read_snapshot() -> AdmissionSnapshot:
    """Fold the fixed hold and all three authoritative host snapshots."""

    hold_value = _json_command((HOLD_HELPER, "--json"))
    if set(hold_value) != {"generation", "sha256", "active"}:
        _deny("malformed hold snapshot")
    try:
        hold = Hold(**hold_value)
    except TypeError:
        _deny("malformed hold snapshot")
    return AdmissionSnapshot(
        hold=hold,
        hosts=tuple(_host_snapshot(host) for host in WORKER_HOSTS),
    )


def _workers() -> tuple[_Worker, ...]:
    """Build the exact three-per-host fixed worker set."""

    workers = []
    for host in WORKER_HOSTS:
        for card_id in WORKER_CARDS[host]:
            agent = f"pi-glm-{host}-{card_id}"
            workers.append(
                _Worker(
                    host=host,
                    card_id=card_id,
                    agent_id=agent,
                    session_id=f"glm-{card_id}",
                    claim_id=f"{card_id}:{agent}",
                    worktree=f"/var/tmp/skcapstone-glm-{card_id}",
                )
            )
    return tuple(workers)


def _require_dependency_clear(workers: Sequence[_Worker]) -> None:
    """Require every fixed card open and every dependency complete."""

    store = CardStore(COORDINATION_HOME)
    cards = {worker.card_id: store.fold(worker.card_id) for worker in workers}
    for worker in workers:
        card = cards[worker.card_id]
        if card is None or card.status.value not in {"backlog", "ready", "in_progress"}:
            _deny("worker card is not claimable")
        if getattr(card, "owner", None):
            _deny("worker card is already claimed")
        for dependency in card.dependencies:
            folded = store.fold(dependency)
            if folded is None or folded.status.value != "done":
                _deny("worker card dependency is not clear")


def _claim(worker: _Worker) -> None:
    """Claim one fixed card using the supported coordination command."""

    result = _run(
        (
            "skcapstone",
            "coord",
            "claim",
            worker.card_id,
            "--agent",
            worker.agent_id,
            "--home",
            str(COORDINATION_HOME),
        )
    )
    if result.returncode != 0:
        _deny("worker claim failed")


def _release(worker: _Worker) -> None:
    """Release one claim and require a successful supported command."""

    result = _run(
        (
            "skcapstone",
            "coord",
            "release-claim",
            worker.card_id,
            "--owner",
            worker.agent_id,
            "--agent",
            "glm-admission@chiap08",
            "--home",
            str(COORDINATION_HOME),
        )
    )
    if result.returncode != 0:
        _deny("worker claim release failed")


def _prepare(worker: _Worker) -> None:
    """Create one idle tmux session without starting Pi or provider work."""

    result = _run(
        (
            "ssh",
            "-oBatchMode=yes",
            worker.host,
            "tmux",
            "new-session",
            "-d",
            "-s",
            worker.session_id,
            "-c",
            worker.worktree,
        )
    )
    if result.returncode != 0:
        _deny("worker session prepare failed")


def _release_worker(worker: _Worker) -> None:
    """Release one prepared session to run the fixed Pi command."""

    prompt = f"Work only claimed card {worker.card_id}. Preserve the active GLM hold."
    command = " ".join(
        shlex.quote(part)
        for part in (
            "pi",
            "--provider",
            "zai",
            "--model",
            MODEL,
            "--session-id",
            worker.session_id,
            "--name",
            worker.agent_id,
            prompt,
        )
    )
    remote = f"tmux send-keys -t {shlex.quote(worker.session_id)} " f"{shlex.quote(command)} Enter"
    result = _run(("ssh", "-oBatchMode=yes", worker.host, remote))
    if result.returncode != 0:
        _deny("worker session release failed")


def _stop(worker: _Worker) -> None:
    """Stop one prepared session and require a successful command."""

    result = _run(
        (
            "ssh",
            "-oBatchMode=yes",
            worker.host,
            "tmux",
            "kill-session",
            "-t",
            worker.session_id,
        )
    )
    if result.returncode != 0:
        _deny("worker session stop failed")


def _rollback(
    workers: Sequence[_Worker], prepared: Sequence[_Worker], reserved: bool
) -> tuple[_RollbackFailure, ...]:
    """Attempt every compensator and return sanitized structured failures."""

    failures: list[_RollbackFailure] = []
    for worker in reversed(prepared):
        try:
            _stop(worker)
        except BaseException as error:
            failures.append(_RollbackFailure("stop", worker.card_id, type(error).__name__))
    if reserved:
        try:
            _abort_wave(tuple(worker.binding() for worker in workers), datetime.now(timezone.utc))
        except BaseException as error:
            failures.append(_RollbackFailure("abort", None, type(error).__name__))
    for worker in reversed(workers):
        try:
            _release(worker)
        except BaseException as error:
            failures.append(_RollbackFailure("release", worker.card_id, type(error).__name__))
    return tuple(failures)


def run_consumer() -> int:
    """Run the fixed consumer, accepting no caller-controlled execution inputs."""

    if socket.gethostname().lower().rstrip(".") != AUTHORITY_HOST:
        _deny("physical host is not chiap08")
    if not ENABLED_MARKER.is_file():
        _deny("GLM admission consumer is disabled")

    workers = _workers()
    _require_dependency_clear(workers)
    claimed: list[_Worker] = []
    prepared: list[_Worker] = []
    reserved = False
    try:
        for worker in workers:
            _claim(worker)
            claimed.append(worker)
        for worker in workers:
            prepared.append(worker)
            _prepare(worker)
        _admit_wave(
            bindings=tuple(worker.binding() for worker in workers),
            snapshot_reader=_read_snapshot,
            now=datetime.now(timezone.utc),
        )
        reserved = True
        for worker in workers:
            _release_worker(worker)
        return 0
    except BaseException as error:
        failures = _rollback(claimed, prepared, reserved)
        if failures:
            raise _RollbackFailed(failures) from error
        raise


def main() -> int:
    """Console entrypoint with no command-line or injectable authority surface."""

    if len(sys.argv) != 1:
        print("DENIED: GLM admission accepts no arguments")
        return 2
    try:
        return run_consumer()
    except AdmissionDenied as error:
        print(f"DENIED: {error}")
        return 2
