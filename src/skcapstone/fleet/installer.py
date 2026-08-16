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


class Frozen(RuntimeError):
    """Raised by run_install when `apply` is attempted while the fleet-wide
    kill-switch (store.is_frozen) is on. `check` is never affected: a report
    is not actuation and freeze must never blind an operator to drift."""


class ActuationNotAllowed(RuntimeError):
    """Raised by run_install when `apply` is attempted without opt-in
    (store.actuation_allowed is False). Distinct from Frozen so a caller can
    tell "the fleet-wide switch is off" apart from any other reason actuation
    is not currently permitted."""


def _result_dict(result: InstallResult) -> dict:
    """Flatten one InstallResult (and its nested InstallStep) into a plain
    JSON-able dict. Dataclasses are not JSON-serializable by default, and
    run_install's return value has to survive `json.dumps` for the CLI and
    any caller that logs or transmits it."""
    return {
        "name": result.step.name,
        "kind": result.step.kind,
        "tier": result.step.tier,
        "backend_id": result.step.backend_id,
        "status": result.status,
        "detail": result.detail,
    }


#: Step statuses that count as a successful outcome for run_install's overall
#: `ok`. Anything else (failed, needs_manual, skipped, warn, or a future
#: status) means the apply did not fully land, so `ok` must go False rather
#: than silently treat a partial install as clean.
_OK_STEP_STATUSES = frozenset({"ok", "would-write"})


def _refresh_inventory(paths) -> None:
    """Re-observe this node and republish node.json (best-effort).

    Called once after a successful, non-dry-run apply so the synced node
    object reflects what was just installed instead of waiting for
    sknoded's own 15-minute inventory re-observe window
    (sknoded.INVENTORY_INTERVAL_S). This is purely a freshness optimization
    on top of sknoded's own report loop, never the install's source of
    truth, so a failure here (no sknoded set up yet, unwritable fleet tree,
    ...) must never fail the install itself: the steps already ran.
    """
    try:
        from . import sknoded
        from .paths import self_node_name

        sknoded.reset_inventory_cache()
        sknoded.run_once(paths, self_node_name())
    except Exception:
        pass


def run_install(
    paths,
    role: str,
    *,
    mode: str,
    dry_run: bool,
    enable: bool,
    start: bool,
    only: list[str] | None,
    backends: dict,
) -> dict:
    """Top-level entry point: diff, gate, and (in apply mode) actuate.

    `check` only ever reads: it builds the drift report and summarizes it,
    and is always allowed to run, freeze included, because a report is not
    actuation and freeze must never blind an operator to drift.

    `apply` first checks store.is_frozen (raises Frozen) and then
    store.actuation_allowed (raises ActuationNotAllowed) before touching
    anything. Once past both gates it builds the InstallPlan from the
    missing_required items and executes it through `apply()`. Only when
    every step landed (ok/would-write) and this was not a dry run does it
    republish this node's inventory, so the synced node object reflects
    the install without waiting for sknoded's own cycle.

    Args:
        paths: FleetPaths for the synced fleet tree.
        role: The profile name (Node spec's `role` field) to install for.
        mode: "check" (report only) or "apply" (actuate).
        dry_run: Passed through to `apply()`; also suppresses the
            post-apply inventory refresh (nothing was actually installed).
        enable: Passed through to `apply()`.
        start: Passed through to `apply()`.
        only: Optional subset of item names to install; passed to `plan()`.
        backends: Backend registry passed through to `apply()`.

    Returns:
        A JSON-able summary: ``{"role", "mode", "results": [...], "ok"}``.
        In "check" mode, each result is one drift finding
        ``{"grade", "category", "name"}``; `ok` is True only when nothing is
        missing_required. In "apply" mode, each result is one flattened
        InstallResult; `ok` is True only when every step's status is
        "ok" or "would-write".

    Raises:
        ValueError: `mode` is neither "check" nor "apply".
        Frozen: `apply` was requested while the fleet is frozen.
        ActuationNotAllowed: `apply` was requested while actuation is not
            opted in (and the fleet is not frozen).
    """
    if mode not in ("check", "apply"):
        raise ValueError(f"mode must be 'check' or 'apply', got {mode!r}")

    if mode == "check":
        drift = load_drift(paths, role)
        results = [
            {"grade": grade, "category": category, "name": name}
            for grade, category, name in drift.findings()
        ]
        ok = not (drift.missing_required_units or drift.missing_required_packages)
        return {"role": role, "mode": "check", "results": results, "ok": ok}

    # mode == "apply": gate BEFORE computing drift. is_frozen/actuation_allowed
    # must short-circuit without ever touching the store's profile/inventory
    # reads, so a frozen or non-opted-in refusal never depends on `paths`
    # supporting anything beyond the freeze file (spec's own contract).
    if store.is_frozen(paths):
        raise Frozen(role)
    if not store.actuation_allowed(paths):
        raise ActuationNotAllowed(role)

    drift = load_drift(paths, role)
    install_plan = plan(drift, only=only)
    install_results = apply(install_plan, backends, dry_run=dry_run, enable=enable, start=start)
    results = [_result_dict(r) for r in install_results]
    ok = all(r["status"] in _OK_STEP_STATUSES for r in results)

    if ok and not dry_run:
        _refresh_inventory(paths)

    return {"role": role, "mode": "apply", "results": results, "ok": ok}
