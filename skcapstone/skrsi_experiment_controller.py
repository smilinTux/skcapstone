"""Proposal-only SKRSI experiment lifecycle controller.

The controller owns lifecycle semantics, while CardStore remains the durable
append-only event log.  Lifecycle events and evidence are deliberately written
as separate events: a state transition is never evidence of its own outcome.
No method in this module actuates a rollout or a recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from skcoord.card_store import card_mutation_lock


class ExperimentLifecycleError(ValueError):
    """Invalid, stale, unsafe, or over-budget lifecycle request."""


STATES = (
    "hypothesis",
    "experiment",
    "intervention_proposal",
    "evaluation",
    "decision",
    "rollout_observation",
    "regression",
    "recovery",
)
_ALLOWED = {
    None: {"hypothesis"},
    "hypothesis": {"experiment"},
    "experiment": {"intervention_proposal", "evaluation"},
    "intervention_proposal": {"evaluation"},
    "evaluation": {"decision", "regression"},
    "decision": {"rollout_observation", "regression"},
    "rollout_observation": {"evaluation", "regression"},
    "regression": {"recovery"},
    "recovery": {"evaluation", "decision"},
}


@dataclass(frozen=True)
class TransitionResult:
    experiment_id: str
    state: str
    revision: int
    transition_id: str
    replayed: bool = False


class ExperimentController:
    """Coordinate proposal-only experiment transitions on a CardStore.

    ``evidence`` is a mapping of metadata references and is stored in its own
    CardStore event.  Callers must provide a stable ``transition_id``; retries
    with the same natural key are exactly-once and return the original result.
    """

    def __init__(self, store: Any, *, agent: str, default_budget: Mapping[str, int] | None = None):
        self.store = store
        self.agent = agent
        self.default_budget = dict(default_budget or {"transitions": 32, "observations": 1000})

    def _events(self, experiment_id: str) -> list[dict[str, Any]]:
        reader = getattr(self.store, "_read_events", None)
        if reader is None:
            raise ExperimentLifecycleError("store does not expose event reads")
        return list(reader(experiment_id))

    def _prior(self, experiment_id: str, transition_id: str) -> TransitionResult | None:
        events = self._events(experiment_id)
        transition = next(
            (
                event
                for event in events
                if event.get("action") == "experiment_transition"
                and event.get("transition_id") == transition_id
            ),
            None,
        )
        evidence = next(
            (
                event
                for event in events
                if event.get("action") == "experiment_evidence"
                and event.get("transition_id") == f"{transition_id}:evidence"
                and event.get("source_transition_id") == transition_id
            ),
            None,
        )
        if transition is not None and evidence is not None:
            return TransitionResult(
                experiment_id,
                transition["state"],
                int(transition["revision"]),
                transition_id,
                True,
            )
        return None

    def _current(self, experiment_id: str) -> tuple[str | None, int, list[dict[str, Any]]]:
        events = self._events(experiment_id)
        transitions = [e for e in events if e.get("action") == "experiment_transition"]
        if not transitions:
            return None, 0, events
        latest = max(transitions, key=lambda e: int(e.get("revision", 0)))
        return latest["state"], int(latest["revision"]), events

    def transition(
        self,
        experiment_id: str,
        state: str,
        *,
        revision: int,
        transition_id: str,
        evidence: Mapping[str, Any],
        payload: Mapping[str, Any] | None = None,
        budget: Mapping[str, int] | None = None,
    ) -> TransitionResult:
        if state not in STATES or not transition_id or not experiment_id:
            raise ExperimentLifecycleError("invalid lifecycle identifiers")
        if not isinstance(evidence, Mapping) or not evidence:
            raise ExperimentLifecycleError("an independent evidence mapping is required")
        replay = self._prior(experiment_id, transition_id)
        if replay:
            return replay
        limits = {**self.default_budget, **dict(budget or {})}
        common = {
            "transition_id": transition_id,
            "revision": revision,
            "state": state,
            "experiment_id": experiment_id,
        }
        # Two independent append-only records.  The evidence event does not
        # carry lifecycle authority and lifecycle state does not imply a verdict.
        # Hold the CardStore card lock across validation and both records so
        # concurrent writers cannot pass the same revision fence.
        home = getattr(self.store, "home", None)
        if home is None:
            raise ExperimentLifecycleError("store has no shared home for locking")
        with card_mutation_lock(home, experiment_id):
            # Re-check after acquiring the lock: another writer may have won.
            replay = self._prior(experiment_id, transition_id)
            if replay:
                return replay
            events = self._events(experiment_id)
            transition = next(
                (
                    event
                    for event in events
                    if event.get("action") == "experiment_transition"
                    and event.get("transition_id") == transition_id
                ),
                None,
            )
            if transition is not None:
                self.store.append_event(
                    experiment_id,
                    "experiment_evidence",
                    self.agent,
                    transition_id=f"{transition_id}:evidence",
                    source_transition_id=transition_id,
                    revision=int(transition["revision"]),
                    experiment_id=experiment_id,
                    evidence=dict(evidence),
                )
                return TransitionResult(
                    experiment_id,
                    transition["state"],
                    int(transition["revision"]),
                    transition_id,
                    True,
                )
            current, actual_revision, events = self._current(experiment_id)
            if revision != actual_revision + 1:
                raise ExperimentLifecycleError(
                    f"revision fence: expected {actual_revision + 1}, got {revision}"
                )
            if state not in _ALLOWED.get(current, set()):
                raise ExperimentLifecycleError(f"invalid transition {current!r} -> {state!r}")
            if len(
                [event for event in events if event.get("action") == "experiment_transition"]
            ) >= int(limits["transitions"]):
                raise ExperimentLifecycleError("transition budget exhausted")
            if state == "rollout_observation" and len(
                [event for event in events if event.get("action") == "experiment_evidence"]
            ) >= int(limits["observations"]):
                raise ExperimentLifecycleError("observation budget exhausted")
            self.store.append_event(
                experiment_id,
                "experiment_transition",
                self.agent,
                **common,
                payload=dict(payload or {}),
                proposal_only=True,
            )
            self.store.append_event(
                experiment_id,
                "experiment_evidence",
                self.agent,
                transition_id=f"{transition_id}:evidence",
                source_transition_id=transition_id,
                revision=revision,
                experiment_id=experiment_id,
                evidence=dict(evidence),
            )
        return TransitionResult(experiment_id, state, revision, transition_id)

    def regression(
        self,
        experiment_id: str,
        *,
        revision: int,
        transition_id: str,
        evidence: Mapping[str, Any],
        containment_proposal: Mapping[str, Any],
        payload: Mapping[str, Any] | None = None,
    ) -> TransitionResult:
        result = self.transition(
            experiment_id,
            "regression",
            revision=revision,
            transition_id=transition_id,
            evidence=evidence,
            payload={
                **dict(payload or {}),
                "containment_proposal": dict(containment_proposal),
                "actuation": "forbidden",
            },
        )
        return result

    def recovery_proposal(
        self,
        experiment_id: str,
        *,
        revision: int,
        transition_id: str,
        evidence: Mapping[str, Any],
        proposal: Mapping[str, Any],
    ) -> TransitionResult:
        return self.transition(
            experiment_id,
            "recovery",
            revision=revision,
            transition_id=transition_id,
            evidence=evidence,
            payload={"recovery_proposal": dict(proposal), "actuation": "forbidden"},
        )
