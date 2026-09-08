"""Deterministic regression coverage for governed Ready review dispatch."""

from __future__ import annotations

import ast
import collections
import hashlib
import json
import re
from pathlib import Path

import pytest

from skcapstone.scheduler_decision import SchedulerDecision, SchedulerFacts, classify_scheduler
from skcapstone.seat_boundaries import BoundaryError

from .test_skfleet_claimability import _event, _load_claimability

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
FIXTURE = ROOT / "tests" / "fixtures" / "skfleet-review-e125b710.json"
CAPACITY_FIXTURE = ROOT / "tests" / "fixtures" / "skfleet-review-capacity-20260908.json"


def _function(name: str, namespace: dict[str, object]) -> object:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name
    )
    exec(compile(ast.Module([node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace[name]


def _fixture_core() -> tuple[dict[str, object], dict[str, object]]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    core = {
        "id": fixture["card_id"],
        "kind": "task",
        "title": "[REVIEW] Verify vendor manifest and locked tests",
        "initial_labels": fixture["labels"],
        "dependencies": [fixture["parent_id"]],
        "links": fixture["links"],
    }
    return fixture, core


def _ready_events(core: dict[str, object]) -> list[dict[str, object]]:
    events = [_event("2026-09-08T05:00:00Z", "jarvis", "move", column="ready")]
    for index, (key, value) in enumerate(core["links"].items(), start=1):
        events.append(
            _event(
                f"2026-09-08T05:00:{index:02d}Z",
                "jarvis",
                "link",
                link_key=key,
                link_value=value,
            )
        )
    return events


def test_e125b710_ready_fixture_is_claimable_exactly_once() -> None:
    fixture, core = _fixture_core()
    namespace = _load_claimability()
    state = namespace["_fold_claimability"](core, _ready_events(core))

    assert namespace["_claimability_reason"](core, state) == "claimable"
    metadata = namespace["_governed_review_metadata"](core, state["labels"])
    assert metadata == (
        fixture["producer_identity"],
        fixture["links"]["candidate_evidence_sha256"],
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("incomplete", "review_incomplete"),
        ("malformed", "review_malformed"),
        ("qwen", "review_qwen_only"),
        ("seat", "review_seat_bound"),
        ("not_ready", "review_not_ready"),
    ],
)
def test_review_exclusions_have_one_stable_reason(mutation: str, reason: str) -> None:
    _fixture, core = _fixture_core()
    namespace = _load_claimability()
    if mutation == "incomplete":
        core["links"] = {**core["links"]}
        core["links"].pop("candidate_patch_sha256")
    elif mutation == "malformed":
        core["links"] = {**core["links"], "candidate_commit": "not-a-commit"}
    elif mutation == "qwen":
        core["initial_labels"] = [*core["initial_labels"], "qwen-only"]
    elif mutation == "seat":
        core["initial_labels"] = [*core["initial_labels"], "seat-link"]
    else:
        state = namespace["_fold_claimability"](core, [])
        assert namespace["_claimability_reason"](core, state) == reason
        return
    events = _ready_events(core)
    state = namespace["_fold_claimability"](core, events)

    assert namespace["_claimability_reason"](core, state) == reason


def test_dependency_and_stale_owner_precede_review_admission() -> None:
    _fixture, core = _fixture_core()
    namespace = _load_claimability()
    ready = _ready_events(core)
    state = namespace["_fold_claimability"](core, ready)
    namespace["_dep_satisfied"] = lambda _dep: False
    assert namespace["_claimability_reason"](core, state) == "dependency"

    claimed = namespace["_fold_claimability"](
        core,
        [
            *ready,
            _event(
                "2026-09-08T05:01:00Z",
                "reviewer",
                "claim",
                owner="reviewer",
                claim_revision="revision-1",
            ),
        ],
    )
    assert namespace["_claimability_reason"](core, claimed) == "owned-doing"
    assert classify_scheduler(SchedulerFacts("e125b710", owner_health="stale")).primary_reason == (
        "owned_stale"
    )


def test_review_batch_partition_reconciles_before_and_after_claim() -> None:
    dispatchable = _function("_pool_v2_dispatchable", {"re": re})
    plan = _function(
        "_review_batch_plan",
        {"_pool_v2_dispatchable": dispatchable, "collections": collections},
    )
    _fixture, core = _fixture_core()
    admission = {
        "card_id": "e125b710",
        "claimable": True,
        "reason": "claimable",
        "host_pin": None,
        "title": core["title"],
        "labels": core["initial_labels"],
        "core": core,
        "review_candidate": True,
        "governed_review": True,
        "overlay": {"reason": "claimable"},
        "source_revision": hashlib.sha256(b"e125b710").hexdigest(),
    }
    before = plan(
        [SchedulerDecision("e125b710", "ready", True)],
        {"e125b710": admission},
        1,
    )
    after = plan(
        [SchedulerDecision("e125b710", "owned_live", False)],
        {"e125b710": {**admission, "claimable": False, "reason": "owned-doing"}},
        1,
    )

    assert before == {
        "population": 1,
        "eligible": 1,
        "batch": 1,
        "reasons": {},
        "eligible_ids": ("e125b710",),
    }
    assert after["population"] == 1
    assert after["eligible"] == after["batch"] == 0
    assert after["reasons"] == {"owned_live": 1}


def test_invalid_reviews_do_not_consume_authoritative_free_lanes() -> None:
    """Regress the 00:40:50 free-slot versus no-free-lane contradiction."""
    observed = json.loads(CAPACITY_FIXTURE.read_text(encoding="utf-8"))
    select_lane = _function(
        "select_compatible_lane",
        {
            "lane_compatibility": _function(
                "lane_compatibility",
                {"_LANE_ONLY_LABELS": {"codex-only": "codex"}},
            )
        },
    )
    remaining = {
        name: values["capacity"] - values["used"] for name, values in observed["slots"].items()
    }
    assert sum(remaining.values()) == observed["total_free"] == 7
    assert observed["rotation_report_sha256"] == (
        "5ca8a18a95ea6990695bfec31b2a7bdfe10997df263ee484a11360b3d8b80b6f"
    )
    assert set(observed["observed_ready_ids"]) == {"b7e8094c", "a13c7010"}
    assert observed["review_batch_eligible"] == 0
    assert observed["lane_defer"] == {
        "no-free-lane:codex": 3,
        "no-free-lane:qwen": 2,
        "no-free-lane:qwen,glm,codex": 3,
    }

    # Rejected reviews never enter the authoritative rows, so they cannot reserve
    # a lane that the later review-assignment boundary will refuse.
    rejected_admissions = [
        {"card_id": "b7e8094c", "claimable": False, "reason": "review_incomplete"},
        {"card_id": "a13c7010", "claimable": False, "reason": "review_malformed"},
    ]
    before = dict(remaining)
    for admission in rejected_admissions:
        if admission["claimable"]:
            remaining["codex"] -= 1
    assert remaining == before

    lane, reason = select_lane(
        ["review", "independent-review", "codex-only"],
        False,
        ["qwen", "glm", "codex", "kimi", "escalate"],
        remaining,
        True,
        False,
    )
    assert (lane, reason) == ("codex", "compatible")
    remaining[lane] -= 1
    assert remaining["codex"] == 1
    assert sum(remaining.values()) == 6


def test_terminal_producer_verdict_controls_dependency_admission() -> None:
    dependency = _function(
        "_dep_satisfied",
        {
            "lifecycle_state": lambda _card: "complete",
            "_load_outcomes": lambda: {
                "good": ("2026-09-08T05:00:00Z", "PASS_FOR_REVIEW"),
                "blocked": ("2026-09-08T05:00:00Z", "BLOCKED dependency"),
            },
            "re": re,
        },
    )

    assert dependency("good") is True
    assert dependency("blocked") is False


def test_preclaim_requires_card_isolated_reviewer_identity() -> None:
    handoff = _function(
        "_pool_v2_preclaim_handoff",
        {
            "_pool_v2_preclaim_matches": lambda _selected, _fresh: True,
            "_review_assignment": lambda cid, _core, _labels, reviewer: (cid, reviewer),
            "BoundaryError": BoundaryError,
        },
    )
    fresh = {"core": {}, "labels": ["review", "independent-review"]}

    with pytest.raises(BoundaryError, match="not isolated"):
        handoff("e125b710", fresh, fresh, "codex-reviewer")
    assert handoff("e125b710", fresh, fresh, "codex-reviewer-e125b710") == (
        "e125b710",
        "codex-reviewer-e125b710",
    )
