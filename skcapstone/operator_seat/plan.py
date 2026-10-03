"""Operator decision layer: turn proposals into dispositioned, classified plans.

Pure. For each proposal the operator reasoned out, this enriches it with the
action catalog's metadata, classifies it (policy), and decides auto-apply vs
escalate-for-approval. No actuation and no I/O; the loop parks the escalations
and (only when execution is explicitly enabled) applies the auto ones.
"""

from __future__ import annotations

from typing import Any, Callable

from .policy import classify_change


def plan_actions(
    proposals: list[dict],
    explain: dict,
    *,
    author: str = "operator",
    target_known: Callable[[dict], bool] | None = None,
    action_allowed: Callable[[dict], bool] | None = None,
) -> list[dict[str, Any]]:
    """Classify and dispose each proposal.

    Returns a list of {proposal, classification, disposition} where disposition
    is 'auto' (auto_approvable) or 'escalate' (needs a human). An action not in
    the app's catalog is treated as unknown metadata and, lacking a standard
    claim or reversibility, will not be auto_approvable.
    """
    catalog = {a["name"]: a for a in explain.get("actions", [])}
    planned: list[dict[str, Any]] = []
    for p in proposals:
        meta = catalog.get(p.get("action"), {})
        reversible = bool(meta.get("reversible", False))
        action = {
            "name": p.get("action"),
            "standard": bool(meta.get("standard")),
            "blast_radius": meta.get("blast_radius"),
            "risk": "low" if reversible else "high",
            "rollback_plan": "revert via controller reconcile" if reversible else "",
            "author": author,
        }
        classification = classify_change(action)
        disposition = "auto" if classification["auto_approvable"] else "escalate"
        # Validate the TARGET, not just the action. The action is checked against
        # the catalog above; without this the object was never checked at all, so
        # a proposal naming something that does not exist still classified auto
        # and was handed to the act verb. Escalating (never auto-applying) an
        # unresolvable target keeps a human in the loop instead of failing at
        # actuation time. Opt-in: callers without fleet access pass nothing and
        # get the previous behavior exactly.
        unresolved = bool(
            disposition == "auto" and target_known is not None and not target_known(p)
        )
        if unresolved:
            disposition = "escalate"
        binding_denied = bool(
            disposition == "auto" and action_allowed is not None and not action_allowed(p)
        )
        if binding_denied:
            disposition = "escalate"
        planned.append(
            {
                "proposal": p,
                "classification": classification,
                "disposition": disposition,
                "unresolved_target": unresolved,
                "binding_denied": binding_denied,
            }
        )
    return planned
