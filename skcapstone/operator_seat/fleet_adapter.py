"""Fleet operator adapter: the reference implementation of the adapter contract.

The one operator observes and acts on every app through the same three verbs
(explain, observe, act). This module is the fleet's conformant implementation of
the read side (explain + observe), gathered from the Phase 4-7 controllers. The
act verb is wired in the fleet adapter CLI card; observe here writes nothing.
"""

from __future__ import annotations

import logging

from ..fleet import (
    agent_controller,
    config_controller,
    cron_controller,
    modelserver_controller,
    node_controller,
    service_controller,
    store,
)
from ..fleet.paths import self_node_name

logger = logging.getLogger(__name__)

#: Reversible ops the operator may apply through the fleet act verb, mapped to
#: the object kind they annotate. Irreversible or major actions never reach the
#: act verb (they escalate and park), so they are deliberately absent here.
_ACTION_KIND = {
    "rerun_cronjob": "cronjob",
    "restart_service": "service",
    "replace_workload": "service",
}

#: Condition types that indicate a PROBLEM when their status is "True" (the rest
#: are health conditions that indicate a problem when "False"). The operator
#: brief uses this polarity to decide what is firing.
PROBLEM_WHEN_TRUE = frozenset(
    {
        "MissedRun",
        "ConfigDrift",
        "RotationOverdue",
        "CrashLooping",
        "SyncConflict",
        "MemoryPressure",
        "DiskPressure",
    }
)

#: The fleet action catalog the operator may draw on (adapter-contract shape).
_ACTIONS = [
    {
        "name": "rerun_cronjob",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "re-run a missed CronJob",
        "kedb_refs": [],
    },
    {
        "name": "restart_service",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart a failed Service unit",
        "kedb_refs": [],
    },
    {
        "name": "replace_workload",
        "standard": False,
        "reversible": True,
        "blast_radius": "medium",
        "runbook": "re-place a workload on another node",
        "kedb_refs": [],
    },
    {
        "name": "drain_node",
        "standard": False,
        "reversible": True,
        "blast_radius": "drain_always_on",
        "runbook": "drain a node (parks if always-on)",
        "kedb_refs": [],
    },
    {
        "name": "delete_object",
        "standard": False,
        "reversible": False,
        "blast_radius": "delete",
        "runbook": "tombstone a fleet object",
        "kedb_refs": [],
    },
]


def _b(value: bool) -> str:
    return "True" if value else "False"


def fleet_explain() -> dict:
    """The fleet's self-description in the adapter-contract shape."""
    from ..fleet.explain import KINDS

    conditions = sorted(
        {
            "MissedRun",
            "AgentReady",
            "Serving",
            "SecretPresent",
            "ConfigDrift",
            "RotationOverdue",
            "Ready",
        }
        | set(PROBLEM_WHEN_TRUE)
    )
    return {
        "kinds": sorted(KINDS),
        "conditions": conditions,
        "actions": [dict(a) for a in _ACTIONS],
    }


def fleet_observe(paths, now_iso: str) -> dict:
    """Read-only snapshot of every fleet object's key condition(s).

    Never writes: the operator's observe verb only reads derived state.
    """
    conds: list[dict] = []

    for nv in node_controller.node_views(paths):
        for c in nv.conditions:
            conds.append({"type": c.get("type"), "status": c.get("status"), "object": nv.name})

    for row in cron_controller.cron_rows(paths, now_iso):
        conds.append({"type": "MissedRun", "status": _b(row.missed), "object": row.name})

    for row in agent_controller.agent_rows(paths, now_iso):
        conds.append({"type": "AgentReady", "status": row.ready, "object": row.name})

    for row in modelserver_controller.modelserver_rows(paths, now_iso):
        conds.append({"type": "Serving", "status": _b(row.serving), "object": row.name})

    for row in config_controller.config_rows(paths, now_iso):
        conds.append(
            {"type": "SecretPresent", "status": _b(row.secrets_present), "object": row.name}
        )
        conds.append({"type": "ConfigDrift", "status": _b(row.drift), "object": row.name})
        conds.append(
            {"type": "RotationOverdue", "status": _b(row.rotation_overdue), "object": row.name}
        )

    for row in service_controller.service_rows(paths):
        conds.append({"type": "Ready", "status": row.ready, "object": row.name})

    return {"conditions": conds}


def fleet_target_known(paths, proposal: dict) -> bool:
    """True when the proposal's object resolves to an existing fleet object.

    The decision layer validates the ACTION against the catalog but had no way
    to check the TARGET, so a proposal naming something that does not exist
    still classified auto and was handed to the act verb, which then raised.
    Actions with no ops-channel mapping return True: the action-level check
    already governs those, and this must not second-guess that disposition.
    """
    kind = _ACTION_KIND.get(proposal.get("action"))
    if kind is None:
        return True
    return store.read_spec(paths, kind, proposal.get("object")) is not None


def _acting_identity() -> str:
    """The capauth identity of the seat performing this action.

    Split out so the failure policy lives at the call site: attribution must
    never be the reason the fleet cannot act.
    """
    from capauth import resolve_agent_identity

    ident = resolve_agent_identity()
    return getattr(ident, "capauth_uri", "") or getattr(ident, "agent", "") or ""


def _operator_action_entry(
    *, action: str, now_iso: str, classification: dict, proposal: dict
) -> dict:
    """One operatorActions record, attributed to the RESOLVED acting identity.

    `by` was the literal string "atlas". An audit line naming a constant
    attributes nothing: every seat, on every node, forever, claims to be the
    same actor, so the field cannot answer the only question an audit entry
    exists to answer. It now carries the capauth identity.

    A resolver failure degrades to "unattributed" rather than raising: an entry
    that says it does not know who acted is honest and still records that
    something acted. Refusing to act because attribution failed would trade a
    provenance gap for an availability outage, which is the wrong trade for an
    ops channel (permissive posture, spec section 7).
    """
    try:
        by = _acting_identity() or "unattributed"
    except Exception as exc:  # noqa: BLE001
        logger.warning("fleet_adapter: identity resolve failed, entry unattributed: %s", exc)
        by = "unattributed"
    return {
        "action": action,
        "ts": now_iso,
        "by": by,
        "changeClass": classification.get("change_class"),
        "rationale": proposal.get("rationale", ""),
    }


def fleet_act(paths, proposal: dict, classification: dict, *, now_iso: str, writer=None) -> dict:
    """Apply an operator proposal to the fleet: the act verb (ops channel).

    Records the action as an attributed entry on the target object's spec
    (`operatorActions`), so every operator touch is auditable and reversible.
    The ENTRY itself is not signed; the spec WRITE carries the signature, in
    the writer block (`writer.signature`, suite named by `writer.suite_id`),
    which covers the whole payload including this entry. Saying the entry is
    signed would invite trust it has not earned.
    Refuses when the fleet is frozen (belt-and-suspenders: the loop already
    checks freeze first). Only reversible ops are mapped; anything else raises,
    since majors and irreversible actions escalate and never reach the act verb.
    The writer is the autonomous seat (agent_seat=True): it may write object
    specs but never plane files.
    """
    if store.is_frozen(paths):
        raise RuntimeError("fleet is frozen: the operator does not actuate")
    action = proposal.get("action")
    kind = _ACTION_KIND.get(action)
    if kind is None:
        raise ValueError(f"no ops-channel mapping for action {action!r}")
    name = proposal.get("object")
    existing = store.read_spec(paths, kind, name)
    if existing is None:
        raise ValueError(f"unknown {kind} object {name!r}")
    spec = dict(existing.get("spec", {}))
    log = list(spec.get("operatorActions", []))
    entry = _operator_action_entry(
        action=action, now_iso=now_iso, classification=classification, proposal=proposal
    )
    # A standing condition (an app that is down reads stale, so the brief is
    # never quiet) re-proposes the same fix every pass. Escalations already
    # dedupe on a content-based decision id so a standing issue is ONE decision
    # a human resolves once; the act path had no equivalent and appended a fresh
    # entry every 15 minutes, growing operatorActions without bound. Collapse a
    # repeat of the immediately preceding action into count + lastTs, so the
    # signal survives ("still happening, N times") without the unbounded log.
    prev = log[-1] if log else None
    if prev and prev.get("action") == action and prev.get("changeClass") == entry["changeClass"]:
        collapsed = dict(prev)
        collapsed["count"] = int(collapsed.get("count", 1)) + 1
        collapsed["lastTs"] = now_iso
        collapsed["rationale"] = entry["rationale"]
        log[-1] = collapsed
    else:
        entry["count"] = 1
        log.append(entry)
    spec["operatorActions"] = log
    writer = writer or store.Writer(
        role="operator",
        node=self_node_name(),
        identity=store.resolved_writer_identity(),
        agent_seat=True,
    )
    return store.write_spec(paths, kind, name, spec, writer=writer)


__all__ = ["fleet_explain", "fleet_observe", "fleet_act", "PROBLEM_WHEN_TRUE"]
