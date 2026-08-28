"""Strict fixed-path snapshot input for the chiap08 GLM consumer."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from .glm_admission import AUTHORITY_DIRECTORY, WORKER_HOSTS, Hold, QueueSample

ENABLE_PATH = AUTHORITY_DIRECTORY / "consumer.enabled.json"
SNAPSHOT_PATHS = {
    host: AUTHORITY_DIRECTORY / "snapshots" / f"{host}.json" for host in WORKER_HOSTS
}
ENABLE_SCHEMA = "skcapstone.glm-consumer-enable.v1"
SNAPSHOT_SCHEMA = "skcapstone.glm-host-snapshot.v1"


class ConsumerDenied(RuntimeError):  # noqa: N818
    """Raised when the consumer cannot prove a complete safe launch."""


@dataclass(frozen=True)
class CardCandidate:
    """One card reported by its assigned authoritative host snapshot."""

    card_id: str
    title: str
    dependency_verdict: str
    human_gate: bool
    claim: dict[str, str] | None


@dataclass(frozen=True)
class PressureSample:
    """Queue and rate-limit evidence used to stop further work."""

    queued: int
    responses_429: int


@dataclass(frozen=True)
class HostSnapshot:
    """Strictly parsed contents of one fixed host snapshot."""

    host: str
    observed_at: str
    reachable: bool
    glm_auto_sessions: int
    hold: Hold
    queue_samples: tuple[QueueSample, QueueSample]
    pressure_samples: tuple[PressureSample, PressureSample]
    cards: tuple[CardCandidate, ...]


def deny(reason: str) -> NoReturn:
    """Fail closed with a stable consumer reason."""

    raise ConsumerDenied(reason)


def integer(value: object, field: str) -> int:
    """Require a non-negative, non-boolean integer."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        deny(f"invalid {field}")
    return value


def read_regular(path: Path, label: str) -> bytes:
    """Read one fixed owner-only regular file without following symlinks."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            deny(f"unsafe {label}")
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                deny(f"unsafe {label}")
            data = bytearray()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 1_000_000:
                    deny(f"oversized {label}")
            return bytes(data)
        finally:
            os.close(fd)
    except ConsumerDenied:
        raise
    except OSError:
        deny(f"missing or unsafe {label}")


def load_json(path: Path, label: str) -> dict[str, object]:
    """Load an exact JSON object from a fixed safe file."""

    try:
        value = json.loads(read_regular(path, label))
    except (UnicodeError, json.JSONDecodeError):
        deny(f"malformed {label}")
    if not isinstance(value, dict):
        deny(f"malformed {label}")
    return value


def enabled() -> bool:
    """Return false for absent enablement and reject malformed enablement."""

    if not ENABLE_PATH.exists():
        return False
    value = load_json(ENABLE_PATH, "consumer enablement")
    if set(value) != {"schema", "enabled", "generation"}:
        deny("invalid consumer enablement")
    if value["schema"] != ENABLE_SCHEMA or value["enabled"] is not True:
        deny("invalid consumer enablement")
    integer(value["generation"], "consumer generation")
    return True


def _parse_hold(value: object) -> Hold:
    """Parse an exact hold object without offering any write operation."""

    if not isinstance(value, dict) or set(value) != {"generation", "sha256", "active"}:
        deny("malformed host snapshot hold")
    generation, sha256, active = value["generation"], value["sha256"], value["active"]
    if not isinstance(generation, str) or not generation:
        deny("malformed host snapshot hold")
    if not isinstance(sha256, str) or not isinstance(active, bool):
        deny("malformed host snapshot hold")
    return Hold(generation=generation, sha256=sha256, active=active)


def _parse_queue(
    value: object,
) -> tuple[tuple[QueueSample, QueueSample], tuple[PressureSample, PressureSample]]:
    """Parse exactly two queue samples, retaining 429 evidence."""

    if not isinstance(value, list) or len(value) != 2:
        deny("malformed host snapshot queue samples")
    queues: list[QueueSample] = []
    pressures: list[PressureSample] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "observed_at",
            "active",
            "queued",
            "responses_429",
        }:
            deny("malformed host snapshot queue samples")
        observed_at = item["observed_at"]
        if not isinstance(observed_at, str):
            deny("malformed host snapshot queue samples")
        active = integer(item["active"], "queue active")
        queued = integer(item["queued"], "queue queued")
        responses_429 = integer(item["responses_429"], "queue responses_429")
        queues.append(QueueSample(observed_at=observed_at, active=active, queued=queued))
        pressures.append(PressureSample(queued=queued, responses_429=responses_429))
    return (queues[0], queues[1]), (pressures[0], pressures[1])


def _parse_card(value: object) -> CardCandidate:
    """Parse one candidate, including any claim even when it is stale."""

    if not isinstance(value, dict) or set(value) != {
        "card_id",
        "title",
        "dependency_verdict",
        "human_gate",
        "claim",
    }:
        deny("malformed host snapshot card")
    card_id, title = value["card_id"], value["title"]
    verdict, human_gate, claim = value["dependency_verdict"], value["human_gate"], value["claim"]
    if (
        not isinstance(card_id, str)
        or not card_id
        or not isinstance(title, str)
        or not title
        or not isinstance(verdict, str)
        or not isinstance(human_gate, bool)
    ):
        deny("malformed host snapshot card")
    if claim is not None:
        if not isinstance(claim, dict) or set(claim) != {"owner", "claim_id", "observed_at"}:
            deny("malformed host snapshot claim")
        if any(not isinstance(item, str) or not item for item in claim.values()):
            deny("malformed host snapshot claim")
    return CardCandidate(card_id, title, verdict, human_gate, claim)


def read_host_snapshot(host: str) -> HostSnapshot:
    """Read and validate the fixed snapshot assigned to one approved host."""

    value = load_json(SNAPSHOT_PATHS[host], f"{host} snapshot")
    if set(value) != {
        "schema",
        "host",
        "observed_at",
        "reachable",
        "glm_auto_sessions",
        "hold",
        "queue_samples",
        "cards",
    }:
        deny("malformed host snapshot")
    if value["schema"] != SNAPSHOT_SCHEMA or value["host"] != host:
        deny("conflicting host snapshot authority")
    observed_at, reachable = value["observed_at"], value["reachable"]
    if not isinstance(observed_at, str) or not isinstance(reachable, bool):
        deny("malformed host snapshot")
    queues, pressures = _parse_queue(value["queue_samples"])
    cards_value = value["cards"]
    if not isinstance(cards_value, list):
        deny("malformed host snapshot cards")
    return HostSnapshot(
        host=host,
        observed_at=observed_at,
        reachable=reachable,
        glm_auto_sessions=integer(value["glm_auto_sessions"], "glm session count"),
        hold=_parse_hold(value["hold"]),
        queue_samples=queues,
        pressure_samples=pressures,
        cards=tuple(_parse_card(item) for item in cards_value),
    )


def read_bundle() -> tuple[HostSnapshot, ...]:
    """Read all three authoritative snapshots and require shared evidence."""

    reports = tuple(read_host_snapshot(host) for host in WORKER_HOSTS)
    if len({report.hold for report in reports}) != 1:
        deny("conflicting host snapshot holds")
    if len({report.queue_samples for report in reports}) != 1:
        deny("conflicting host snapshot queues")
    return reports
