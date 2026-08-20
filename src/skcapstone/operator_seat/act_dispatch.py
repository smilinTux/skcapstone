"""Atlas STANDARD-catalog physical actuation dispatch (CR-9.1, autonomy step 1).

Wires the ``--execute`` HONOR path for the STANDARD catalog on the fleet and the
skchat adapters ONLY. It is OFF by default: the loop's ``apply_fn`` stays the
signed-annotation ``fleet_act`` path unless honoring is deliberately enabled
(Chef's one-line flip: ``--honor`` / ``SKOPERATOR_HONOR=1``). Turning it off is
byte-identical to the report/annotate deployment running today.

When honoring is on, an auto-approved action:

1. is recorded as an ITIL change record first (governance: every operator
   action is an ITIL change, per the Atlas Constitution), then
2. physically actuates through the tested ``actuator.honor`` systemd path
   (fleet ``restart_service`` on the object's unit, or the skchat adapter's
   reversible standard restarts).

Only the two step-1 adapters participate (``HONOR_ADAPTERS``). Irreversible or
major actions never reach here: ``policy.classify_change`` forces them to
escalate before the loop ever calls ``apply_fn``. Freeze always wins: each act
verb (and ``actuator.honor``) refuses when the fleet is frozen.
"""

from __future__ import annotations

from typing import Any, Callable

from . import actuator, fleet_adapter, itil_intent, skchat_adapter

#: The adapters that participate in autonomy step 1 (fleet is the reference,
#: skchat is the first app adapter). Anything else is observe-only until a later
#: ramp step widens this set.
HONOR_ADAPTERS = ("fleet", "skchat")

_FLEET_ACTIONS = {a["name"] for a in fleet_adapter._ACTIONS}
_SKCHAT_ACTIONS = {a["name"] for a in skchat_adapter._ACTIONS}


def route_action(action: str | None) -> str | None:
    """Which honor adapter owns an action name, or None when unmapped.

    skchat wins a (non-existent today) name clash because its actions are
    hyphenated and the fleet's are underscored, so the two sets are disjoint.
    """
    if action in _SKCHAT_ACTIONS:
        return "skchat"
    if action in _FLEET_ACTIONS:
        return "fleet"
    return None


def merged_explain() -> dict[str, Any]:
    """The union of the fleet and skchat action catalogs for reasoning + planning.

    When honoring is on, the proposer and the planner must see BOTH apps' actions
    (so the brain can propose ``restart-telegram-bridge`` and the planner can
    classify it), not just the fleet's. When honoring is off this is never used,
    so the report/annotate loop keeps its fleet-only catalog byte-identically.
    """
    fe = fleet_adapter.fleet_explain()
    se = skchat_adapter.skchat_explain()
    actions: dict[str, dict] = {a["name"]: a for a in fe.get("actions", [])}
    for a in se.get("actions", []):
        actions.setdefault(a["name"], a)
    return {
        "kinds": sorted(set(fe.get("kinds", [])) | set(se.get("kinds", []))),
        "conditions": sorted(set(fe.get("conditions", [])) | set(se.get("conditions", []))),
        "actions": list(actions.values()),
    }


def _change_type(classification: dict) -> str:
    """Map the operator change class onto an ITIL ChangeType value.

    STANDARD stays STANDARD (auto-approves at fold); NORMAL (the auto-normal
    tier) records as NORMAL. Majors/emergency never reach the honor path.
    """
    return "standard" if classification.get("change_class") == "standard" else "normal"


def build_apply_fn(
    paths,
    now_iso: str,
    *,
    runner=None,
    itil=None,
    emit: Callable[[str], Any] = lambda _m: None,
) -> Callable[[dict, dict], dict]:
    """Build the honor ``apply_fn(proposal, classification)`` for the loop.

    The returned function is only ever invoked by the loop for auto-dispositioned
    proposals when execution is on. For each one it records an ITIL change (when
    an ``ITILManager`` is supplied) and then physically actuates via the routed
    adapter. ``runner`` is injectable so tests and the gameday drill capture the
    systemd command WITHOUT executing it. Freeze is enforced by the act verbs.
    """

    def apply_fn(proposal: dict, classification: dict) -> dict:
        action = proposal.get("action")
        adapter_name = route_action(action)
        if adapter_name not in HONOR_ADAPTERS:
            raise ValueError(f"no honor adapter for action {action!r} (step-1: {HONOR_ADAPTERS})")

        rollback = proposal.get("rollback_plan") or "revert via controller reconcile"
        record = itil_intent.build_change_record(
            {"name": action}, classification, dry_run="false", rollback_plan=rollback
        )
        change_id = None
        if itil is not None:
            chg = itil.propose_change(
                title=record["title"],
                change_type=_change_type(classification),
                risk=record["risk"],
                rollback_plan=rollback,
                created_by="operator",
                tags=list(record["tags"]),
            )
            change_id = getattr(chg, "id", None)
            emit(f"itil: change {change_id} recorded for {action!r}")

        if adapter_name == "skchat":
            actuation = skchat_adapter.skchat_act(paths, proposal, classification, runner=runner)
        else:  # fleet: annotate the signed intent, then physically honor it.
            fleet_adapter.fleet_act(paths, proposal, classification, now_iso=now_iso)
            unit = proposal.get("unit") or proposal.get("object")
            actuation = actuator.honor(
                paths,
                {"action": "restart_service", "ts": proposal.get("ts")},
                unit,
                runner=runner,
            )
        if not isinstance(actuation, dict) or actuation.get("performed") is not True:
            reason = (
                actuation.get("reason", "actuator did not perform action")
                if isinstance(actuation, dict)
                else "invalid actuator response"
            )
            raise RuntimeError(f"actuation failed: {reason}")
        emit(f"honor: {adapter_name} {action!r} -> {actuation}")
        return {
            "adapter": adapter_name,
            "change_id": change_id,
            "record": record,
            "actuation": actuation,
        }

    return apply_fn


__all__ = ["HONOR_ADAPTERS", "route_action", "merged_explain", "build_apply_fn"]
