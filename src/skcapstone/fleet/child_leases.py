"""Pure child progress lease configuration and evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

DEFAULT_STARTUP_LEASE_S = 120.0
DEFAULT_FIRST_OUTPUT_LEASE_S = 300.0
DEFAULT_PROVIDER_RESPONSE_LEASE_S = 600.0
DEFAULT_PROGRESS_LEASE_S = 900.0


@dataclass(frozen=True)
class ChildLeaseConfig:
    """Independent, bounded leases for one exact child generation."""

    startup_s: float = DEFAULT_STARTUP_LEASE_S
    first_output_s: float = DEFAULT_FIRST_OUTPUT_LEASE_S
    provider_response_s: float = DEFAULT_PROVIDER_RESPONSE_LEASE_S
    progress_s: float = DEFAULT_PROGRESS_LEASE_S

    def __post_init__(self) -> None:
        values = (
            self.startup_s,
            self.first_output_s,
            self.provider_response_s,
            self.progress_s,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in values
        ):
            raise ValueError("child leases must be finite and positive")

    def as_dict(self) -> dict[str, float]:
        """Return the stable persisted configuration shape."""
        return {
            "startup_s": self.startup_s,
            "first_output_s": self.first_output_s,
            "provider_response_s": self.provider_response_s,
            "progress_s": self.progress_s,
        }

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "ChildLeaseConfig":
        """Load one complete persisted configuration, failing closed."""
        expected = {"startup_s", "first_output_s", "provider_response_s", "progress_s"}
        if set(values) != expected:
            raise ValueError("child lease configuration fields are incomplete")
        if any(
            isinstance(values[key], bool) or not isinstance(values[key], (int, float))
            for key in expected
        ):
            raise ValueError("child lease configuration values are invalid")
        try:
            return cls(**{key: float(values[key]) for key in expected})
        except (TypeError, ValueError) as exc:
            raise ValueError("child lease configuration values are invalid") from exc


@dataclass(frozen=True)
class ChildLeaseObservation:
    """Read-only timestamps and identity for a child lease evaluation."""

    card: str
    owner: str
    claim_revision: str
    host: str
    lane: str
    model_bucket: str
    phase: str
    started_at: float
    startup_complete_at: float | None = None
    first_output_at: float | None = None
    last_output_at: float | None = None
    provider_started_at: float | None = None
    last_progress_at: float | None = None
    wrapper_heartbeat_at: float | None = None
    child_alive: bool = True
    side_effects: bool = False
    human_gate: bool = False
    terminal: bool = False
    superseded: bool = False
    ambiguous_progress: bool = False
    child_pid: int | None = None
    child_start_ticks: int | None = None


@dataclass(frozen=True)
class ChildLeaseReceipt:
    """Non-secret, machine-readable lease result. No prompt or output data."""

    card: str
    owner: str
    claim_revision: str
    host: str
    lane: str
    model_bucket: str
    phase: str
    elapsed_s: float
    lease_s: float
    state: str
    reason: str
    child_pid: int | None = None
    child_start_ticks: int | None = None


def evaluate_child_lease(
    observation: ChildLeaseObservation,
    *,
    now: float,
    config: ChildLeaseConfig = ChildLeaseConfig(),
) -> ChildLeaseReceipt:
    """Classify child progress, not wrapper liveness, using monotonic seconds."""
    if now < observation.started_at:
        raise ValueError("monotonic clock moved backwards")
    phase = observation.phase
    completed = {
        "startup": observation.startup_complete_at,
        "first-output": observation.first_output_at,
        "provider-response": observation.first_output_at,
        "progress": None,
    }
    marks = {
        "startup": observation.started_at,
        "first-output": observation.startup_complete_at or observation.started_at,
        "provider-response": observation.provider_started_at,
        "progress": observation.last_progress_at,
    }
    limits = {
        "startup": config.startup_s,
        "first-output": config.first_output_s,
        "provider-response": config.provider_response_s,
        "progress": config.progress_s,
    }
    mark = marks.get(phase)
    limit = limits.get(phase)
    if mark is None or limit is None:
        return ChildLeaseReceipt(
            observation.card,
            observation.owner,
            observation.claim_revision,
            observation.host,
            observation.lane,
            observation.model_bucket,
            phase,
            0.0,
            0.0,
            "ambiguous",
            "unknown-phase-or-missing-progress",
            observation.child_pid,
            observation.child_start_ticks,
        )
    elapsed = max(0.0, now - mark)
    protected = (
        observation.side_effects
        or observation.human_gate
        or observation.terminal
        or observation.superseded
        or observation.ambiguous_progress
    )
    if protected:
        state, reason = "not-replayable", "protected-or-ambiguous"
    elif completed.get(phase) is not None:
        state, reason = "healthy", "phase-complete"
        elapsed = max(0.0, float(completed[phase]) - mark)
    elif not observation.child_alive:
        state, reason = "child-exited", "child-not-alive"
    elif elapsed > limit:
        state, reason = "child-stalled", "lease-expired"
    else:
        state, reason = "healthy", "child-progress-within-lease"
    return ChildLeaseReceipt(
        observation.card,
        observation.owner,
        observation.claim_revision,
        observation.host,
        observation.lane,
        observation.model_bucket,
        phase,
        elapsed,
        limit,
        state,
        reason,
        observation.child_pid,
        observation.child_start_ticks,
    )
