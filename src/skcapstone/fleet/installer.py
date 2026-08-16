"""Profile-aware stack installer (orchestrator). See
docs/superpowers/specs/2026-08-16-skfleet-install-orchestrator-design.md."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class InstallStep:
    """Represents a single installation step.

    Attributes:
        name: Name of the step.
        kind: Type of step ("unit" | "package").
        tier: Priority/order tier.
        backend_id: Backend identifier.
    """

    name: str
    kind: str  # "unit" | "package"
    tier: int
    backend_id: str


@dataclass(frozen=True)
class InstallPlan:
    """A plan consisting of multiple installation steps.

    Attributes:
        steps: List of InstallStep objects to execute.
    """

    steps: list[InstallStep] = field(default_factory=list)


@dataclass(frozen=True)
class InstallResult:
    """Result of executing an installation step.

    Attributes:
        step: The InstallStep that was executed.
        status: Status code (ok|wrote|would-write|warn|failed|skipped|needs_manual).
        detail: Optional detail message.
    """

    step: InstallStep
    status: str  # ok|wrote|would-write|warn|failed|skipped|needs_manual
    detail: str = ""
