"""Change class and risk policy classifier for the operator seat (Seat O1a).

Pure mapping from a proposed action's metadata to an ITIL change class plus
a risk level and an auto vs escalate decision. No filesystem or network
access; callers own reading the ratified catalog and any wiring.
"""

from __future__ import annotations

from typing import Any

RATIFIED_STANDARD_CATALOG = frozenset(
    {
        "restart_service",
        "rotate_credential",
        "rotate_log",
        "update_label",
    }
)

IRREVERSIBLE_BLAST_RADII = frozenset({"delete", "drain_always_on", "fleet_restart"})

_RISK_LEVELS = frozenset({"low", "medium", "high"})


def classify_change(action: dict[str, Any]) -> dict[str, Any]:
    """Classify a proposed action into an ITIL change class and risk level.

    Args:
        action: Proposed action metadata. Recognized keys: ``name`` (str),
            ``standard`` (bool, proposer's claim the action is pre-ratified),
            ``reversible`` (bool; an explicit ``False`` forces MAJOR),
            ``blast_radius`` (str), ``risk`` (one of low/medium/high, defaults
            to low), ``freeze`` (bool, marks a fleet freeze/unfreeze action),
            ``rollback_plan`` (any, truthy if a rollback plan is attached),
            and ``author`` (str).

    Returns:
        A dict with ``change_class`` (standard/normal/major/emergency),
        ``risk`` (low/medium/high), and ``auto_approvable`` (bool).
    """
    risk = action.get("risk", "low")
    if risk not in _RISK_LEVELS:
        risk = "low"

    # Irreversibility fires MAJOR two ways: an irreversible blast radius, or an
    # action that explicitly declares itself non-reversible (e.g. a session kill
    # whose blast is low but which cannot be undone). Either forces escalation so
    # it never reaches the auto/act path.
    blast_radius = action.get("blast_radius")
    irreversible = blast_radius in IRREVERSIBLE_BLAST_RADII or action.get("reversible") is False
    if irreversible:
        risk = "high"

    if action.get("freeze"):
        return {"change_class": "emergency", "risk": risk, "auto_approvable": False}

    if irreversible or risk == "high":
        return {"change_class": "major", "risk": risk, "auto_approvable": False}

    if action.get("standard") is True and action.get("name") in RATIFIED_STANDARD_CATALOG:
        return {"change_class": "standard", "risk": risk, "auto_approvable": True}

    auto_approvable = (
        risk != "high" and bool(action.get("rollback_plan")) and action.get("author") == "operator"
    )
    return {"change_class": "normal", "risk": risk, "auto_approvable": auto_approvable}
