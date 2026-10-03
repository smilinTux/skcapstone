"""Agent convergence planning (Phase 5, step 2a).

A PURE function that computes what must change to converge an Agent spec
to observed state: no filesystem, network, or subprocess. Actuating the
plan (writing soul/model/daemon) is a separate step (5.2b).
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent import normalize_agent_spec


@dataclass(frozen=True)
class ConvergeAction:
    """One step needed to converge an Agent's observed state to its spec."""

    kind: str
    target: str
    detail: dict


def agent_convergence_plan(spec: dict, observed: dict) -> list[ConvergeAction]:
    """Diff a normalized Agent spec against observed state.

    Args:
        spec: Raw agent spec dict (normalized internally).
        observed: Agent's current observed state (active_soul, model,
            daemon_ready).

    Returns:
        Actions needed to converge, in soul, model, daemon order. Empty
        when already in sync.
    """
    normalized = normalize_agent_spec(spec)
    name = normalized["name"]
    actions: list[ConvergeAction] = []

    soul = normalized["soul"]
    if soul is not None and soul != observed.get("active_soul"):
        actions.append(ConvergeAction(kind="set_soul", target=name, detail={"soul": soul}))

    model = normalized["model"]
    if model is not None and model != observed.get("model"):
        actions.append(ConvergeAction(kind="set_model", target=name, detail={"model": model}))

    node = normalized["daemon"].get("node")
    if node is not None and not observed.get("daemon_ready"):
        actions.append(ConvergeAction(kind="ensure_daemon", target=name, detail={"node": node}))

    return actions
