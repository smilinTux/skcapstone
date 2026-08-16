"""Profile-aware stack installer (orchestrator). See
docs/superpowers/specs/2026-08-16-skfleet-install-orchestrator-design.md."""
from __future__ import annotations

from dataclasses import dataclass, field

from .profile_doctor import DriftReport
from . import install_backends


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


def plan(drift: DriftReport, *, only: list[str] | None = None) -> InstallPlan:
    """Ordered install steps for the missing_required items only.

    Builds an ordered InstallPlan from a DriftReport by resolving each
    missing_required package and unit to its backend, determining its tier,
    and sorting by (tier, name). Forbidden and unexpected items are ignored.

    Args:
        drift: A DriftReport with missing and forbidden items.
        only: Optional list of item names to include. If provided, only
            items in this set will be included in the plan.

    Returns:
        An InstallPlan with steps sorted by (tier, name).
    """
    wanted = set(only) if only is not None else None
    steps: list[InstallStep] = []
    for pkg in drift.missing_required_packages:
        if wanted is None or pkg in wanted:
            bid = install_backends.resolve(pkg, "package")
            steps.append(InstallStep(pkg, "package", install_backends.tier_of(bid), bid))
    for unit in drift.missing_required_units:
        if wanted is None or unit in wanted:
            bid = install_backends.resolve(unit, "unit")
            steps.append(InstallStep(unit, "unit", install_backends.tier_of(bid), bid))
    steps.sort(key=lambda s: (s.tier, s.name))
    return InstallPlan(steps=steps)
