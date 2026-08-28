"""Disabled-by-default chiap08 consumer for one admitted GLM worker wave.

This module has no provider client. It reads fixed owner-controlled snapshots,
uses the reviewed :func:`glm_admission.admit_wave` protocol unchanged, and
requires a fixed local worker controller to stage then atomically commit all
nine workers. Missing enablement or control support fails closed.
"""

from __future__ import annotations

import json
import socket
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Protocol, Sequence

from . import glm_consumer_snapshots as snapshots
from .glm_admission import (
    AUTHORITY_DIRECTORY,
    AUTHORITY_HOST,
    WORKER_HOSTS,
    AdmissionSnapshot,
    HostReport,
    WorkerBinding,
    admit_wave,
)
from .glm_consumer_snapshots import (
    ENABLE_PATH,
    ConsumerDenied,
    HostSnapshot,
    PressureSample,
)

TRANSCRIPT_DIRECTORY = AUTHORITY_DIRECTORY / "transcripts"
WORKTREE_ROOT = Path("/var/lib/skcapstone-local/glm-worktrees")
WORKER_CONTROL = Path("/usr/local/libexec/skcapstone-glm-worker-control")
CONTROL_SCHEMA = "skcapstone.glm-worker-control.v1"


@dataclass(frozen=True)
class PreparedWorker:
    """Opaque staging token bound to the worker that produced it."""

    binding: WorkerBinding
    token: str
    transcript: Path


class ClaimCoordinator(Protocol):
    """Supported coordination operations needed by the transaction."""

    def claim(self, card_id: str, agent_id: str) -> str:
        """Claim one card and return its distinct claim identity."""

    def release(self, card_id: str, agent_id: str) -> None:
        """Release a claim through the supported coordination command."""


class WaveBackend(Protocol):
    """Staging and atomic-wave operations supplied by worker control."""

    def stage(self, binding: WorkerBinding, transcript: Path) -> PreparedWorker:
        """Prepare one non-running worker and its transcript."""

    def commit(self, prepared: Sequence[PreparedWorker]) -> set[str]:
        """Atomically start all staged workers and return live session IDs."""

    def stop(self, prepared: Sequence[PreparedWorker]) -> None:
        """Stop every staged or live worker without deleting transcripts."""


def _deny(reason: str) -> NoReturn:
    """Fail closed with a stable consumer reason."""

    raise ConsumerDenied(reason)


def _physical_hostname() -> str:
    """Read and normalize the operating-system hostname."""

    try:
        value = socket.gethostname()
    except OSError:
        _deny("physical hostname unavailable")
    if not isinstance(value, str):
        _deny("physical hostname unavailable")
    return value.lower().rstrip(".")


def _admission_snapshot(bundle: tuple[HostSnapshot, ...]) -> AdmissionSnapshot:
    """Project strict host files into the reviewed admission protocol."""

    return AdmissionSnapshot(
        hold=bundle[0].hold,
        hosts=tuple(
            HostReport(item.host, item.reachable, item.glm_auto_sessions, item.observed_at)
            for item in bundle
        ),
        queue_samples=bundle[0].queue_samples,
    )


def _select_cards(bundle: tuple[HostSnapshot, ...]) -> tuple[tuple[str, str], ...]:
    """Select exactly three unclaimed dependency-PASS non-human cards per host."""

    selected: list[tuple[str, str]] = []
    for report in bundle:
        eligible = sorted(
            (
                card
                for card in report.cards
                if card.dependency_verdict == "PASS"
                and not card.human_gate
                and "[human]" not in card.title.lower()
                and card.claim is None
            ),
            key=lambda card: card.card_id,
        )
        if len(eligible) < 3:
            _deny(f"{report.host} lacks three dependency-clear cards")
        selected.extend((report.host, card.card_id) for card in eligible[:3])
    card_ids = [card_id for _, card_id in selected]
    if len(selected) != 9 or len(set(card_ids)) != 9:
        _deny("wave cards are not exactly nine distinct cards")
    return tuple(selected)


def _identity(host: str, card_id: str, generation: int) -> tuple[str, str, str]:
    """Derive fixed distinct worker, session, and worktree identities."""

    safe_card = "".join(
        character for character in card_id if character.isalnum() or character in "-_"
    )
    if safe_card != card_id or not safe_card:
        _deny("unsafe card identity")
    agent = f"glm-{host}-g{generation}-{card_id}"
    session = f"glm-g{generation}-{host}-{card_id}"
    workspace = str(WORKTREE_ROOT / f"g{generation}" / host / card_id)
    return agent, session, workspace


def _run_command(command: Sequence[str], payload: bytes | None) -> bytes:
    """Run a fixed local command and fail closed on any uncertainty."""

    try:
        result = subprocess.run(
            command, input=payload, capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        _deny("required local control command failed")
    if result.returncode != 0:
        _deny("required local control command failed")
    return result.stdout


class CommandClaimCoordinator:
    """Coordination adapter restricted to supported SKCapstone commands."""

    def claim(self, card_id: str, agent_id: str) -> str:
        """Claim through ``skcapstone coord claim`` without a shell."""

        _run_command(("skcapstone", "coord", "claim", card_id, "--agent", agent_id), None)
        return f"{card_id}:{agent_id}"

    def release(self, card_id: str, agent_id: str) -> None:
        """Release through ``skcapstone coord release-claim`` without a shell."""

        _run_command(
            (
                "skcapstone",
                "coord",
                "release-claim",
                card_id,
                "--owner",
                agent_id,
                "--agent",
                AUTHORITY_HOST,
            ),
            None,
        )


def _control(action: str, body: dict[str, object]) -> dict[str, object]:
    """Exchange canonical JSON with the fixed worker-control executable."""

    request = {"schema": CONTROL_SCHEMA, "action": action, **body}
    raw = _run_command((str(WORKER_CONTROL),), json.dumps(request, sort_keys=True).encode())
    try:
        reply = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        _deny("malformed worker control reply")
    if not isinstance(reply, dict) or reply.get("schema") != CONTROL_SCHEMA:
        _deny("malformed worker control reply")
    return reply


class FixedWorkerBackend:
    """Adapter for the fixed, separately installed atomic worker controller."""

    def stage(self, binding: WorkerBinding, transcript: Path) -> PreparedWorker:
        """Stage one worker using a canonical JSON request."""

        reply = _control("stage", {"binding": binding.__dict__, "transcript": str(transcript)})
        token = reply.get("token")
        if not isinstance(token, str) or not token:
            _deny("worker control returned invalid stage token")
        return PreparedWorker(binding, token, transcript)

    def commit(self, prepared: Sequence[PreparedWorker]) -> set[str]:
        """Request one atomic commit and parse its complete live-session set."""

        reply = _control("commit-wave", {"tokens": [item.token for item in prepared]})
        sessions = reply.get("live_sessions")
        if not isinstance(sessions, list) or any(not isinstance(item, str) for item in sessions):
            _deny("worker control returned invalid live sessions")
        return set(sessions)

    def stop(self, prepared: Sequence[PreparedWorker]) -> None:
        """Stop every token while leaving transcript files untouched."""

        _control("stop-wave", {"tokens": [item.token for item in prepared]})


def must_stop(samples: Sequence[PressureSample]) -> bool:
    """Stop on any 429 or positive queue in two consecutive samples."""

    if any(sample.responses_429 > 0 for sample in samples):
        return True
    return len(samples) >= 2 and samples[-2].queued > 0 and samples[-1].queued > 0


def _validate_bindings(bindings: Sequence[WorkerBinding]) -> None:
    """Defend transaction identities before invoking reviewed admission."""

    if len(bindings) != 9 or Counter(binding.host for binding in bindings) != Counter(
        {host: 3 for host in WORKER_HOSTS}
    ):
        _deny("invalid worker cardinality or distribution")
    for field in ("card_id", "agent_id", "session_id", "claim_id", "workspace"):
        if len({getattr(binding, field) for binding in bindings}) != 9:
            _deny(f"duplicate worker {field}")


def _release_all(
    coordinator: ClaimCoordinator, claimed: Sequence[tuple[str, str]], errors: list[str]
) -> None:
    """Release every supported claim and collect failures without stopping early."""

    for card_id, agent_id in reversed(claimed):
        try:
            coordinator.release(card_id, agent_id)
        except Exception as exc:  # noqa: BLE001 - attempt every release
            errors.append(f"claim release failed for {card_id}: {exc}")


def _launch_wave(
    *,
    generation: int,
    now: datetime,
    coordinator: ClaimCoordinator,
    backend: WaveBackend,
) -> tuple[WorkerBinding, ...]:
    """Reserve and atomically launch all nine workers, or roll back and deny."""

    if _physical_hostname() != AUTHORITY_HOST:
        _deny("physical host is not chiap08")
    if not snapshots.enabled():
        _deny("consumer is disabled")
    bundle = snapshots.read_bundle()
    if must_stop(bundle[0].pressure_samples):
        _deny("queue pressure or 429 stopped the wave")
    selected = _select_cards(bundle)
    claimed: list[tuple[str, str]] = []
    prepared: list[PreparedWorker] = []
    try:
        bindings: list[WorkerBinding] = []
        for host, card_id in selected:
            agent, session, workspace = _identity(host, card_id, generation)
            claim_id = coordinator.claim(card_id, agent)
            claimed.append((card_id, agent))
            bindings.append(WorkerBinding(host, card_id, agent, session, claim_id, workspace))
        _validate_bindings(bindings)

        frozen = bundle

        def read_unchanged() -> AdmissionSnapshot:
            """Re-read fixed evidence and reject card or host changes."""

            current = snapshots.read_bundle()
            if current != frozen:
                _deny("authoritative snapshots changed during launch")
            return _admission_snapshot(current)

        admit_wave(
            proposed_generation=generation,
            bindings=tuple(bindings),
            snapshot_reader=read_unchanged,
            now=now,
        )
        for binding in bindings:
            transcript = TRANSCRIPT_DIRECTORY / f"g{generation}-{binding.session_id}.log"
            prepared.append(backend.stage(binding, transcript))
        expected = {binding.session_id for binding in bindings}
        if backend.commit(tuple(prepared)) != expected:
            _deny("partial worker launch")
        return tuple(bindings)
    except Exception as exc:
        errors: list[str] = []
        if prepared:
            try:
                backend.stop(tuple(prepared))
            except Exception as stop_exc:  # noqa: BLE001 - releases must still run
                errors.append(f"worker stop failed: {stop_exc}")
        _release_all(coordinator, claimed, errors)
        detail = f"; {'; '.join(errors)}" if errors else ""
        raise ConsumerDenied(f"wave failed closed: {exc}{detail}") from exc


def consume_once(
    *,
    generation: int,
    coordinator: ClaimCoordinator | None = None,
    backend: WaveBackend | None = None,
    now: datetime | None = None,
) -> tuple[WorkerBinding, ...] | None:
    """Consume one fixed-path wave; absent enablement returns without action."""

    if _physical_hostname() != AUTHORITY_HOST:
        _deny("physical host is not chiap08")
    if not snapshots.enabled():
        return None
    instant = now or datetime.now(timezone.utc)
    return _launch_wave(
        generation=generation,
        now=instant,
        coordinator=coordinator or CommandClaimCoordinator(),
        backend=backend or FixedWorkerBackend(),
    )


def main() -> int:
    """Run the fixed generation supplied by the owner-only enablement file."""

    try:
        if not snapshots.enabled():
            return 0
        enablement = snapshots.load_json(ENABLE_PATH, "consumer enablement")
        generation = snapshots.integer(enablement["generation"], "consumer generation")
        consume_once(generation=generation)
        return 0
    except ConsumerDenied:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
