"""Profile-aware stack installer (orchestrator). See
docs/superpowers/specs/2026-08-16-skfleet-install-orchestrator-design.md."""
from __future__ import annotations

from dataclasses import dataclass, field

from .profile_doctor import DriftReport
from . import install_backends, nodeinventory, profile_doctor, store


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


def apply(plan: InstallPlan, backends: dict, *, dry_run=False, enable=False, start=False) -> list[InstallResult]:
    """Execute each step through its backend; isolate failures per backend.

    Executes an InstallPlan by calling the appropriate backend function for
    each step. A backend that raises or returns 'failed' isolates: that step
    is marked 'failed' and later steps sharing its backend_id are skipped.
    Independent steps still run. UNSUPPORTED backend_id results in
    'needs_manual' status without calling any backend.

    Args:
        plan: The InstallPlan to execute.
        backends: Dict mapping backend_id to callable. Each callable takes
            (names: list[str], *, dry_run, enable, start) and returns
            (status: str, detail: str).
        dry_run: If True, backends will run in dry-run mode.
        enable: If True, backends will enable units.
        start: If True, backends will start units.

    Returns:
        List of InstallResult, one per step in the plan.
    """
    results: list[InstallResult] = []
    failed_backends: set[str] = set()

    for step in plan.steps:
        if step.backend_id == install_backends.UNSUPPORTED:
            results.append(InstallResult(step, "needs_manual", "no backend for this unit"))
            continue

        if step.backend_id in failed_backends:
            results.append(InstallResult(step, "skipped", "a prior step in this backend failed"))
            continue

        fn = backends.get(step.backend_id)
        if fn is None:
            results.append(InstallResult(step, "needs_manual", f"backend {step.backend_id} unregistered"))
            continue

        try:
            status, detail = fn([step.name], dry_run=dry_run, enable=enable, start=start)
        except Exception as exc:
            status, detail = "failed", str(exc)

        if status == "failed":
            failed_backends.add(step.backend_id)

        results.append(InstallResult(step, status, detail))

    return results


class ProfileNotApplied(RuntimeError):
    """Raised by load_drift when a role has no applied profile in the store.

    The applied profile lives in the synced fleet tree
    (``objects/profile/<role>.json``), written by the operator seat via
    ``skfleet apply``. There is deliberately no fallback to the repo's
    shipped ``deploy/fleet-objects/`` manifests here: those are defaults a
    checkout carries, not what this fleet actually agreed to run, and a
    drift report built against a default nobody applied would tell an
    operator to converge a node toward the wrong thing. A role with no
    applied profile is not degraded input, it is an operator error (bind
    the role, or apply its profile) and must surface as one.
    """


def load_drift(paths, role: str, *, inventory: dict | None = None) -> DriftReport:
    """Diff one role's live inventory against its APPLIED profile.

    Reads the profile the fleet actually applied for ``role`` out of the
    synced store (never the repo's shipped manifests) and compares it
    against the node's live inventory, cluster-aware: a caller observing a
    remote node injects that node's inventory, while a node checking itself
    lets this collect its own.

    Args:
        paths: FleetPaths for the synced fleet tree.
        role: The profile name (Node spec's `role` field) to load and check.
        inventory: Observed inventory dict (nodeinventory.collect() shape).
            Injected by callers checking a node other than the local one
            (and by tests); when None, this collects the local node's own
            inventory via nodeinventory.collect().

    Returns:
        A DriftReport comparing `inventory` against the applied profile.

    Raises:
        ProfileNotApplied: No profile named `role` has been applied to the
            store (`store.read_spec` returned None).
    """
    payload = store.read_spec(paths, "profile", role)
    if payload is None:
        raise ProfileNotApplied(role)
    profile_spec = payload.get("spec") or {}
    if inventory is None:
        inventory = nodeinventory.collect()
    return profile_doctor.diff(inventory, profile_spec)
