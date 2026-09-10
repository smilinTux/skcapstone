from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.skrsi_experiment_controller import ExperimentController, ExperimentLifecycleError


def controller(tmp_path, experiment_id: str = "exp00001") -> ExperimentController:
    store = CardStore(tmp_path)
    store.create(CardCore(id=experiment_id, title="Experiment"))
    return ExperimentController(store, agent="atlas")


def test_lifecycle_is_revision_fenced_evidence_bound_and_budgeted(tmp_path) -> None:
    subject = controller(tmp_path)
    result = subject.transition(
        "exp00001",
        "hypothesis",
        revision=1,
        transition_id="hypothesis-1",
        evidence={"sha256": "a" * 64},
    )
    assert (result.state, result.revision, result.replayed) == ("hypothesis", 1, False)
    with pytest.raises(ExperimentLifecycleError, match="revision fence"):
        subject.transition(
            "exp00001",
            "experiment",
            revision=3,
            transition_id="experiment-3",
            evidence={"sha256": "b" * 64},
        )
    with pytest.raises(ExperimentLifecycleError, match="budget exhausted"):
        subject.transition(
            "exp00001",
            "experiment",
            revision=2,
            transition_id="experiment-2",
            evidence={"sha256": "b" * 64},
            budget={"transitions": 1},
        )


def test_replay_is_exactly_once_by_natural_key(tmp_path) -> None:
    subject = controller(tmp_path)
    first = subject.transition(
        "exp00001",
        "hypothesis",
        revision=1,
        transition_id="same-key",
        evidence={"sha256": "a" * 64},
    )
    replay = subject.transition(
        "exp00001",
        "hypothesis",
        revision=1,
        transition_id="same-key",
        evidence={"sha256": "a" * 64},
    )
    events = subject.store._read_events("exp00001")
    assert first.replayed is False and replay.replayed is True
    assert [event["action"] for event in events] == [
        "experiment_transition",
        "experiment_evidence",
    ]
    assert events[1]["source_transition_id"] == "same-key"


def test_retry_repairs_evidence_interrupted_after_transition_append(tmp_path) -> None:
    subject = controller(tmp_path)
    append_event = subject.store.append_event
    interrupted = False

    def interrupt_evidence(*args, **kwargs):
        nonlocal interrupted
        if args[1] == "experiment_evidence" and not interrupted:
            interrupted = True
            raise OSError("deterministic interruption after transition append")
        return append_event(*args, **kwargs)

    subject.store.append_event = interrupt_evidence
    with pytest.raises(OSError, match="deterministic interruption"):
        subject.transition(
            "exp00001",
            "hypothesis",
            revision=1,
            transition_id="interrupted-key",
            evidence={"sha256": "a" * 64},
        )

    subject.store.append_event = append_event
    replay = subject.transition(
        "exp00001",
        "hypothesis",
        revision=1,
        transition_id="interrupted-key",
        evidence={"sha256": "a" * 64},
    )
    second_replay = subject.transition(
        "exp00001",
        "hypothesis",
        revision=1,
        transition_id="interrupted-key",
        evidence={"sha256": "a" * 64},
    )

    events = subject.store._read_events("exp00001")
    transitions = [event for event in events if event["action"] == "experiment_transition"]
    evidence = [event for event in events if event["action"] == "experiment_evidence"]
    assert replay.replayed is True and second_replay.replayed is True
    assert len(transitions) == len(evidence) == 1
    assert evidence[0]["transition_id"] == "interrupted-key:evidence"
    assert evidence[0]["source_transition_id"] == "interrupted-key"


def test_regression_and_recovery_are_non_actuating_proposals(tmp_path) -> None:
    subject = controller(tmp_path)
    for revision, state in enumerate(("hypothesis", "experiment", "evaluation"), 1):
        subject.transition(
            "exp00001",
            state,
            revision=revision,
            transition_id=f"step-{revision}",
            evidence={"sha256": str(revision) * 64},
        )
    subject.regression(
        "exp00001",
        revision=4,
        transition_id="regression-4",
        evidence={"sha256": "d" * 64},
        containment_proposal={"action": "rollback"},
    )
    subject.recovery_proposal(
        "exp00001",
        revision=5,
        transition_id="recovery-5",
        evidence={"sha256": "e" * 64},
        proposal={"action": "restore"},
    )
    transitions = [
        event
        for event in subject.store._read_events("exp00001")
        if event.get("action") == "experiment_transition"
    ]
    assert transitions[-2]["payload"]["actuation"] == "forbidden"
    assert transitions[-1]["payload"]["actuation"] == "forbidden"


def test_concurrent_writers_commit_one_revision(tmp_path) -> None:
    subject = controller(tmp_path)

    def attempt(key: str) -> str:
        try:
            subject.transition(
                "exp00001",
                "hypothesis",
                revision=1,
                transition_id=key,
                evidence={"sha256": key * 64},
            )
            return "committed"
        except ExperimentLifecycleError:
            return "fenced"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ("a", "b")))
    assert sorted(outcomes) == ["committed", "fenced"]
