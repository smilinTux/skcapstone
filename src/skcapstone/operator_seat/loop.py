"""Operator loop: observe, reason, decide, and (only when enabled) act.

One pass: check the freeze first, observe every adapter, triage into a brief,
route to the cheap or the decision brain, let the agent propose, classify each
proposal, then park escalations for a human and (ONLY when execution is
explicitly enabled and an apply function is wired) apply the auto ones. It is
safe by default: with execute=False and apply_fn=None it writes nothing, it
reasons, plans, parks escalations, and reports what it would do.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from typing import Any, Callable

from ..fleet import signing, store
from ..fleet.paths import default_paths
from . import (
    action_ledger,
    adapter,
    brain,
    brief,
    cmdb_adapter,
    decisions,
    fleet_adapter,
    plan,
    safety,
    skchat_adapter,
    skcode_adapter,
    skcomms_adapter,
    skdashboard_adapter,
    skgateway_adapter,
    skmemory_adapter,
    skos_adapter,
)

#: The registered app adapters: name -> observe callable(paths, now_iso) -> {conditions}.
#: One operator, many apps: the fleet is the reference; each app plugs in here.
#: Every app observe fails safe (reports healthy when the app is unreachable), so a
#: down probe never raises a false alarm.
#:
#: skdashboard was registered as an Operatorapp (registration.APP_REGISTRY) but
#: was never added here, so an unfrozen ATLAS never actually observed it: the
#: seat lane read "no-adapter" regardless of what its (also-dead) declared cli
#: said (ATLAS Eyes, card 90b5b277). Wiring it in is the real fix; see
#: ``skdashboard_adapter`` for the read-only, fail-safe probe itself.
ADAPTERS: dict[str, Callable[..., dict]] = {
    "fleet": fleet_adapter.fleet_observe,
    "cmdb": cmdb_adapter.observe,
    "skchat": skchat_adapter.observe,
    "skcode": skcode_adapter.observe,
    "skcomms": skcomms_adapter.observe,
    "skdashboard": skdashboard_adapter.observe,
    "skmemory": skmemory_adapter.observe,
    "skgateway": skgateway_adapter.observe,
    "skos": skos_adapter.observe,
}

#: The adapter modules ADAPTERS draws from, in the same order, so condition
#: POLARITY can be collected from the modules that own it.
_ADAPTER_MODULES = (
    fleet_adapter,
    cmdb_adapter,
    skchat_adapter,
    skcode_adapter,
    skcomms_adapter,
    skdashboard_adapter,
    skmemory_adapter,
    skgateway_adapter,
    skos_adapter,
)

#: Every condition type that indicates a PROBLEM when its status is "True"; the
#: rest are health types that indicate a problem when "False" (brief.build_brief).
#: The fleet's set is the reference, but polarity belongs to whichever adapter
#: declares the condition, so each app adapter may add its own via an optional
#: module-level PROBLEM_WHEN_TRUE (skos' GradingBacklog is the first). Without
#: this union an app's problem-type condition would be read upside down: quiet
#: when it fires, firing when it is quiet.
PROBLEM_WHEN_TRUE = frozenset().union(
    *(getattr(module, "PROBLEM_WHEN_TRUE", frozenset()) for module in _ADAPTER_MODULES)
)

CONDITION_SCHEMAS = {
    name: adapter.condition_schema(
        list(getattr(module, "CONDITIONS", ())),
        problem_when_true=set(getattr(module, "PROBLEM_WHEN_TRUE", frozenset())),
    )
    for name, module in zip(ADAPTERS, _ADAPTER_MODULES)
}


def _no_proposals(brief_dict: dict, route: str) -> list[dict]:
    """Default agent: propose nothing (keeps run_once safe and model-free)."""
    return []


def _decision_id(proposal: dict, now_iso: str, index: int) -> str:
    # Content-based (action + object), NOT time-based: the same persistent firing
    # maps to the same id every pass, so with park's create-or-skip a standing
    # issue is one decision the human resolves once, not a new one each run.
    seed = "|".join(
        str(proposal.get(field) or "") for field in ("app", "condition", "object", "action")
    )
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


def _operatorapp_allows(paths, proposal: dict, *, require_signature: bool = False) -> bool:
    """Require an app-scoped, declared condition and human-ratified action."""
    app = proposal.get("app")
    condition = proposal.get("condition")
    action = proposal.get("action")
    if not all(isinstance(value, str) and value for value in (app, condition, action)):
        return False
    record = store.read_spec(paths, "operatorapp", app)
    if require_signature:
        verifier = signing.capauth_verifier()
        if verifier is None or record is None:
            return False
        verdict, _ = signing.verify_payload(record, verifier)
        if verdict != "verified":
            return False
        expected_generation = str(record.get("generation") or "")
        supplied_generation = proposal.get("catalog_generation")
        if not supplied_generation or str(supplied_generation) != expected_generation:
            return False
    spec = (record or {}).get("spec") or {}
    return condition in spec.get("conditions", []) and action in spec.get(
        "ratifiedStandardActions", []
    )


def _bind_signed_catalog_generation(paths, proposal: dict) -> dict:
    """Bind a proposal to the verified OperatorApp generation, never a fallback."""
    app = proposal.get("app")
    if not isinstance(app, str) or not app:
        return dict(proposal)
    record = store.read_spec(paths, "operatorapp", app)
    verifier = signing.capauth_verifier()
    if record is None or verifier is None:
        return dict(proposal)
    verdict, _ = signing.verify_payload(record, verifier)
    if verdict != "verified" or not record.get("generation"):
        return dict(proposal)
    bound = dict(proposal)
    bound["catalog_generation"] = str(record["generation"])
    return bound


def _condition_firing(condition: dict, problem_types: set[str]) -> bool:
    """Return whether one observed condition is firing under its polarity."""
    status = condition.get("status")
    if status == "Unknown":
        return True
    polarity = condition.get("polarity")
    problem_when_true = (
        polarity == "problem_when_true"
        if polarity in adapter.POLARITIES
        else condition.get("type") in problem_types
    )
    return (problem_when_true and status == "True") or (
        not problem_when_true and status == "False"
    )


def _verify_postcondition(
    observers: dict[str, Callable[..., dict]],
    proposal: dict,
    paths,
    now_iso: str,
    problem_types: set[str],
) -> tuple[bool, str]:
    """Re-observe the owning app and require the bound condition to clear."""
    app = proposal.get("app")
    observer = observers.get(app)
    if observer is None:
        return False, "owning observer unavailable"
    conditions = observer(paths, now_iso).get("conditions", [])
    matches = [
        item
        for item in conditions
        if item.get("type") == proposal.get("condition")
        and (proposal.get("object") is None or item.get("object") == proposal.get("object"))
    ]
    if not matches:
        return False, "bound condition missing after action"
    if any(_condition_firing(item, problem_types) for item in matches):
        return False, "bound condition still firing after action"
    return True, "postcondition verified"


def _run_once(
    paths=None,
    *,
    now_iso: str,
    problem_types: set[str] | None = None,
    propose: Callable[[dict, str], list[dict]] = _no_proposals,
    explain: dict | None = None,
    decisions_dir=None,
    apply_fn: Callable[[dict, dict], Any] | None = None,
    rollback_fn: Callable[[dict, dict, Any], Any] | None = None,
    execute: bool = False,
    emit: Callable[[str], Any] = print,
    extra_observers: dict[str, Callable[..., dict]] | None = None,
    target_known: Callable[[dict], bool] | None = None,
    execution_state: safety.ExecutionState | None = None,
    require_verified_actions: bool = False,
    require_signed_catalog: bool = False,
    lifecycle_ledger: action_ledger.ActionLedger | None = None,
    catalog_generation: str = "operatorapp-current",
    ledger_actor: str = "atlas",
    deadline: float | None = None,
) -> dict:
    """Run one operator pass.

    Safe by default (execute=False, apply_fn=None): reasons, plans, parks
    escalations, reports, and writes nothing to the fleet. Actuation happens ONLY
    when execute is True AND apply_fn is wired AND the disposition is auto AND the
    fleet is not frozen. Escalations park in decisions_dir when given.

    ``extra_observers`` are the manifest-discovered observe adapters (OPS0.3),
    ``{name: observe(paths, now_iso)}``. They are merged UNDER the built-in
    ``ADAPTERS`` (a built-in always wins on a name clash) so discovery only widens
    what Atlas observes. Empty/None (the default, and whenever discovery is gated
    off) makes this byte-identical to the built-in-only pass.

    Returns {frozen, brief, route, proposals, planned, outcomes, report}.
    """
    paths = paths or default_paths()

    # Freeze wins, always, and first: a frozen fleet gets no observation, no
    # reasoning, and no action, only a stand-down report.
    if store.is_frozen(paths):
        report = "operator: freeze is on, standing down. No observation, no action."
        emit(report)
        return {
            "frozen": True,
            "brief": None,
            "route": None,
            "proposals": [],
            "planned": [],
            "outcomes": [],
            "report": report,
        }

    ptypes = problem_types if problem_types is not None else set(PROBLEM_WHEN_TRUE)
    # Discovered observers merge UNDER the built-ins: ADAPTERS spreads last so a
    # built-in always wins a name clash. Each observe fails safe on its own.
    adapters = {**(extra_observers or {}), **ADAPTERS}
    observations = {}
    for name, fn in adapters.items():
        try:
            payload = fn(paths, now_iso)
        except Exception:  # noqa: BLE001 - a probe failure is Unknown, never healthy/crash
            payload = None
        schema = CONDITION_SCHEMAS.get(name)
        if schema is None:
            declared = [
                item.get("type")
                for item in (payload or {}).get("conditions", [])
                if isinstance(item, dict) and isinstance(item.get("type"), str)
            ]
            schema = adapter.condition_schema(declared)
        envelope = adapter.normalize_observe(
            name,
            payload,
            schema,
            observed_at=now_iso,
            provenance=f"operator-adapter:{name}",
        )
        observations[name] = envelope["conditions"]
    if deadline is not None:
        safety.assert_before_deadline(deadline)
    the_brief = brief.build_brief(observations, ptypes)
    route = brain.route_brain(the_brief)
    proposals = list(propose(the_brief, route))
    if require_signed_catalog:
        proposals = [_bind_signed_catalog_generation(paths, item) for item in proposals]

    explain = explain if explain is not None else fleet_adapter.fleet_explain()
    planned = plan.plan_actions(
        proposals,
        explain,
        target_known=target_known,
        action_allowed=(
            (
                lambda proposal: _operatorapp_allows(
                    paths, proposal, require_signature=require_signed_catalog
                )
            )
            if require_verified_actions
            else None
        ),
    )

    outcomes: list[dict] = []
    # Per-PASS memo of resolved occurrences (keyed on the same 9-field base
    # identity ActionIntent.identity() uses at occurrence=0). Two proposals for
    # the same identity within this ONE pass ("same episode, repeated
    # observation") MUST collapse onto the same occurrence even if the first
    # one's lineage reaches a terminal state (e.g. VERIFIED) before the
    # second is examined below -- see ActionLedger.resolve_occurrence's
    # docstring. Cleared automatically every call: a NEW pass gets a fresh
    # memo and so is free to see a truly prior pass's terminal state and
    # advance the occurrence, which is exactly how a genuinely later
    # real-world recurrence earns its own intent.
    occurrence_memo: dict[tuple, int] = {}
    for i, pl in enumerate(planned):
        prop, disp = pl["proposal"], pl["disposition"]
        intent_id: str | None = None
        if lifecycle_ledger is not None:
            created_at = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
            base_identity = {
                "condition_fingerprint": safety.action_fingerprint(prop),
                "application": str(prop.get("app") or "unknown"),
                "target_kind": str(prop.get("target_kind") or "CI"),
                "target_id": str(prop.get("object") or "unknown"),
                "action": str(prop.get("action") or "unknown"),
                "catalog_generation": str(prop.get("catalog_generation") or catalog_generation),
                "itil_change_id": prop.get("change_id") or prop.get("itil_change_id"),
                "cmdb_ci_id": prop.get("ci_id") or prop.get("cmdb_ci_id"),
                "authorization_ref": prop.get("authorization_ref"),
            }
            memo_key = tuple(sorted(base_identity.items()))
            if memo_key in occurrence_memo:
                occurrence = occurrence_memo[memo_key]
            else:
                occurrence = lifecycle_ledger.resolve_occurrence(base_identity)
                occurrence_memo[memo_key] = occurrence
            intent = action_ledger.ActionIntent(
                condition_fingerprint=base_identity["condition_fingerprint"],
                application=base_identity["application"],
                target_kind=base_identity["target_kind"],
                target_id=base_identity["target_id"],
                action=base_identity["action"],
                catalog_generation=base_identity["catalog_generation"],
                created_at=created_at,
                itil_change_id=base_identity["itil_change_id"],
                cmdb_ci_id=base_identity["cmdb_ci_id"],
                verification=dict(prop.get("verification") or {}),
                rollback=dict(prop.get("rollback") or {}),
                authorization_ref=base_identity["authorization_ref"],
                occurrence=occurrence,
            )
            lifecycle_ledger.create(
                intent,
                actor=ledger_actor,
                evidence_ref=prop.get("evidence_ref"),
            )
            intent_id = intent.intent_id
            current = lifecycle_ledger.current_state(intent_id)
            if current is action_ledger.ActionState.OBSERVED:
                lifecycle_ledger.append(
                    intent_id,
                    action_ledger.ActionState.DIAGNOSED,
                    occurred_at=created_at,
                    actor=ledger_actor,
                )
                current = action_ledger.ActionState.DIAGNOSED
            if current is action_ledger.ActionState.DIAGNOSED:
                lifecycle_ledger.append(
                    intent_id,
                    action_ledger.ActionState.PROPOSED,
                    occurred_at=created_at,
                    actor=ledger_actor,
                )
        if disp == "auto" and execute and apply_fn is not None:
            # Per-proposal isolation, mirroring the fail-safe observe side: one bad
            # proposal must not abort the pass. Without this a single raise skipped
            # every later proposal INCLUDING decisions.park, so escalations the human
            # was supposed to rule on were silently never written, and no report was
            # emitted at all. Broad by intent: any actuation failure is contained,
            # recorded on the outcome, and reported.
            attempt_started = False
            result: Any = None
            try:
                if deadline is not None:
                    safety.assert_before_deadline(deadline)
                fingerprint = safety.action_fingerprint(prop)
                if execution_state is not None:
                    eligible, reason = execution_state.eligibility(fingerprint, time.time())
                    if not eligible:
                        raise RuntimeError(f"execution suppressed: {reason}")
                if intent_id is not None:
                    event_at = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                    lifecycle_ledger.append(
                        intent_id,
                        action_ledger.ActionState.AUTHORIZED,
                        occurred_at=event_at,
                        actor=ledger_actor,
                        detail={"disposition": disp},
                    )
                    lifecycle_ledger.append(
                        intent_id,
                        action_ledger.ActionState.EXECUTING,
                        occurred_at=event_at,
                        actor=ledger_actor,
                    )
                attempt_started = True
                result = apply_fn(prop, pl["classification"])
                # All actuation contracts return either a direct ``performed``
                # flag or an honor envelope containing ``actuation``.
                performed = result.get("performed") if isinstance(result, dict) else None
                if isinstance(result, dict) and "actuation" in result:
                    performed = (result.get("actuation") or {}).get("performed")
                if performed is False:
                    raise RuntimeError("actuator reported performed=False")
                proof_required = require_verified_actions or lifecycle_ledger is not None
                if proof_required and performed is not True:
                    raise RuntimeError("actuator omitted performed=True proof")
                if require_verified_actions or lifecycle_ledger is not None:
                    verified, reason = _verify_postcondition(
                        adapters, prop, paths, now_iso, ptypes
                    )
                    if not verified:
                        raise RuntimeError(reason)
                if execution_state is not None:
                    execution_state.record(fingerprint, time.time(), success=True)
                if intent_id is not None:
                    # ActionIntent.itil_change_id is frozen identity, set only
                    # when the PROPOSER pre-supplied change_id/itil_change_id
                    # (see base_identity above); it cannot be rewritten after
                    # the intent exists without silently changing what
                    # identity it hashes to. When the honor apply_fn instead
                    # AUTO-creates its own ITIL change (act_dispatch.
                    # build_apply_fn, no proposer-supplied change_id), that
                    # change's id is only known once apply_fn returns here --
                    # record it on this durable, append-only VERIFIED event so
                    # the correlation is never lost, even though the frozen
                    # intent core itself stays proposer-scoped.
                    auto_change_id = result.get("change_id") if isinstance(result, dict) else None
                    lifecycle_ledger.append(
                        intent_id,
                        action_ledger.ActionState.VERIFIED,
                        occurred_at=datetime.fromisoformat(now_iso.replace("Z", "+00:00")),
                        actor=ledger_actor,
                        detail={"itil_change_id": auto_change_id} if auto_change_id else {},
                    )
                outcome = "verified" if require_verified_actions else "applied"
            except Exception as exc:
                if execution_state is not None and attempt_started:
                    try:
                        execution_state.record(
                            safety.action_fingerprint(prop),
                            time.time(),
                            success=False,
                            reason=str(exc),
                        )
                    except Exception as state_exc:
                        # Persistence is a safety control, so its failure keeps
                        # the action failed, but must not hide later proposals
                        # or prevent the human escalation from being parked.
                        exc = RuntimeError(f"{exc}; state persistence failed: {state_exc}")
                if decisions_dir is not None:
                    decisions.park(
                        decisions_dir,
                        [prop],
                        decision_id=_decision_id(prop, now_iso, i),
                        created_iso=now_iso,
                    )
                rollback_result: Any = None
                rollback_error: Exception | None = None
                if attempt_started and rollback_fn is not None and prop.get("rollback"):
                    try:
                        rollback_result = rollback_fn(prop, pl["classification"], result)
                        rollback_performed = (
                            rollback_result.get("performed")
                            if isinstance(rollback_result, dict)
                            else None
                        )
                        if rollback_performed is not True:
                            raise RuntimeError("rollback omitted performed=True proof")
                    except Exception as rb_exc:  # noqa: BLE001 - contain rollback failure
                        rollback_error = rb_exc
                if (
                    intent_id is not None
                    and lifecycle_ledger.current_state(intent_id)
                    is action_ledger.ActionState.EXECUTING
                ):
                    event_at = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                    # apply_fn's own exception (e.g. act_dispatch's "actuation
                    # failed: ...") loses its return value, but build_apply_fn
                    # stamps the ITIL change it already opened onto the
                    # exception (`exc.change_id`) before raising, precisely so
                    # this correlation survives a failed attempt too. A later
                    # step's exception (postcondition/performed-proof checks)
                    # instead leaves `result` populated with apply_fn's return.
                    auto_change_id = getattr(exc, "change_id", None)
                    if auto_change_id is None and isinstance(result, dict):
                        auto_change_id = result.get("change_id")
                    fail_detail = {"reason": str(exc)}
                    if auto_change_id:
                        fail_detail["itil_change_id"] = auto_change_id
                    lifecycle_ledger.append(
                        intent_id,
                        action_ledger.ActionState.FAILED,
                        occurred_at=event_at,
                        actor=ledger_actor,
                        detail=fail_detail,
                    )
                    if rollback_result is not None and rollback_error is None:
                        lifecycle_ledger.append(
                            intent_id,
                            action_ledger.ActionState.ROLLED_BACK,
                            occurred_at=event_at,
                            actor=ledger_actor,
                            detail={"result": rollback_result},
                        )
                    else:
                        lifecycle_ledger.append(
                            intent_id,
                            action_ledger.ActionState.ESCALATED,
                            occurred_at=event_at,
                            actor=ledger_actor,
                            detail={
                                "decision_parked": decisions_dir is not None,
                                "rollback_error": str(rollback_error) if rollback_error else None,
                            },
                        )
                outcome = (
                    f"failed then rolled back: {exc}"
                    if rollback_result is not None and rollback_error is None
                    else f"failed: {exc}"
                )
        elif disp == "auto":
            outcome = "auto-ready (execution off)"
        elif decisions_dir is not None:
            decisions.park(
                decisions_dir,
                [prop],
                decision_id=_decision_id(prop, now_iso, i),
                created_iso=now_iso,
            )
            outcome = "escalated (parked for approval)"
        else:
            outcome = "escalate (no decision store)"
        outcomes.append(
            {
                "action": prop.get("action"),
                "disposition": disp,
                "outcome": outcome,
                "intent_id": intent_id,
            }
        )

    report = brain.format_report(the_brief, proposals)
    if outcomes:
        report += "\ndispositions: " + "; ".join(f"{o['action']} {o['outcome']}" for o in outcomes)
    emit(report)
    return {
        "frozen": False,
        "brief": the_brief,
        "route": route,
        "proposals": proposals,
        "planned": planned,
        "outcomes": outcomes,
        "report": report,
    }


def run_once(
    paths=None,
    *,
    max_runtime_seconds: float = 300.0,
    execution_state: safety.ExecutionState | None = None,
    **kwargs,
) -> dict:
    """Run one bounded pass, with a nonblocking single-flight lock when state is supplied."""
    deadline = safety.monotonic_deadline(max_runtime_seconds)
    if execution_state is None:
        return _run_once(paths, execution_state=None, deadline=deadline, **kwargs)
    with execution_state.single_flight():
        return _run_once(paths, execution_state=execution_state, deadline=deadline, **kwargs)
