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
from typing import Any, Callable

from ..fleet import store
from ..fleet.paths import default_paths
from . import (
    brain,
    brief,
    decisions,
    fleet_adapter,
    plan,
    skchat_adapter,
    skcode_adapter,
    skcomms_adapter,
    skgateway_adapter,
    skmemory_adapter,
    skos_adapter,
)

#: The registered app adapters: name -> observe callable(paths, now_iso) -> {conditions}.
#: One operator, many apps: the fleet is the reference; each app plugs in here.
#: Every app observe fails safe (reports healthy when the app is unreachable), so a
#: down probe never raises a false alarm.
ADAPTERS: dict[str, Callable[..., dict]] = {
    "fleet": fleet_adapter.fleet_observe,
    "skchat": skchat_adapter.observe,
    "skcode": skcode_adapter.observe,
    "skcomms": skcomms_adapter.observe,
    "skmemory": skmemory_adapter.observe,
    "skgateway": skgateway_adapter.observe,
    "skos": skos_adapter.observe,
}


def _no_proposals(brief_dict: dict, route: str) -> list[dict]:
    """Default agent: propose nothing (keeps run_once safe and model-free)."""
    return []


def _decision_id(proposal: dict, now_iso: str, index: int) -> str:
    # Content-based (action + object), NOT time-based: the same persistent firing
    # maps to the same id every pass, so with park's create-or-skip a standing
    # issue is one decision the human resolves once, not a new one each run.
    seed = f"{proposal.get('action')}|{proposal.get('object')}"
    return hashlib.sha1(seed.encode()).hexdigest()[:12]


def run_once(
    paths=None,
    *,
    now_iso: str,
    problem_types: set[str] | None = None,
    propose: Callable[[dict, str], list[dict]] = _no_proposals,
    explain: dict | None = None,
    decisions_dir=None,
    apply_fn: Callable[[dict, dict], Any] | None = None,
    execute: bool = False,
    emit: Callable[[str], Any] = print,
) -> dict:
    """Run one operator pass.

    Safe by default (execute=False, apply_fn=None): reasons, plans, parks
    escalations, reports, and writes nothing to the fleet. Actuation happens ONLY
    when execute is True AND apply_fn is wired AND the disposition is auto AND the
    fleet is not frozen. Escalations park in decisions_dir when given.

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

    ptypes = problem_types if problem_types is not None else set(fleet_adapter.PROBLEM_WHEN_TRUE)
    observations = {
        name: fn(paths, now_iso).get("conditions", []) for name, fn in ADAPTERS.items()
    }
    the_brief = brief.build_brief(observations, ptypes)
    route = brain.route_brain(the_brief)
    proposals = list(propose(the_brief, route))

    explain = explain if explain is not None else fleet_adapter.fleet_explain()
    planned = plan.plan_actions(proposals, explain)

    outcomes: list[dict] = []
    for i, pl in enumerate(planned):
        prop, disp = pl["proposal"], pl["disposition"]
        if disp == "auto" and execute and apply_fn is not None:
            apply_fn(prop, pl["classification"])
            outcome = "applied"
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
        outcomes.append({"action": prop.get("action"), "disposition": disp, "outcome": outcome})

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
