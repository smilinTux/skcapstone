"""Regression tests for the fleet launcher's authoritative claimability fold."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_claimability() -> dict[str, object]:
    """Load the dependency-free fold without executing the launcher."""
    names = {
        "_coord_task_claimable",
        "_dependency_value",
        "_fold_claimability",
        "_claimability_reason",
        "_authoritative_card_state",
        "authoritative_claimability",
    }
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(nodes) == names
    namespace: dict[str, object] = {
        "HOST": "chiap03",
        "CARDS": "/unused",
        "_COLUMNS": {"backlog", "ready", "doing", "review", "done"},
        "_NOT_CLAIMABLE": {"not-claimable", "sprint-container", "do-not-claim"},
        "_SENSITIVE_CATEGORY": re.compile(
            r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
            r"deploy|production|release|migrat)",
            re.I,
        ),
        "_CATEGORY_OPT_IN": "dispatch-approved",
        "_dep_satisfied": lambda _dep: True,
        "host_pin": lambda _core, _labels: None,
        "json": json,
        "non_implementation": lambda core, labels: (
            "[HUMAN]" in str(core.get("title") or "").upper() or "human-gate" in labels
        ),
        "os": os,
        "_legacy_claimability_events": lambda fresh=False: {},
        "_strict_card_events": lambda _cid, fresh=False: [],
    }
    module = ast.Module(body=[nodes[name] for name in names], type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def _core(card_id: str, *, labels: list[str] | None = None) -> dict[str, object]:
    return {
        "id": card_id,
        "kind": "task",
        "title": f"Synthetic claimability fixture {card_id}",
        "initial_labels": labels or [],
        "dependencies": [],
    }


def _event(ts: str, writer: str, action: str, **values: object) -> dict[str, object]:
    return {"ts": ts, "writer": writer, "seq": 0, "action": action, **values}


def _claim(ts: str, writer: str, revision: str) -> dict[str, object]:
    return _event(ts, writer, "claim", owner=writer, claim_revision=revision)


def _release(ts: str, writer: str, owner: str, revision: str) -> dict[str, object]:
    return _event(
        ts,
        writer,
        "release_claim",
        released_owner=owner,
        expected_claim_revision=revision,
    )


@pytest.mark.parametrize(
    ("card_id", "labels", "events", "expected"),
    [
        (
            "600fc649",
            [],
            [
                _claim("2026-08-29T01:11:52Z", "reviewer", "rev-a"),
                _claim("2026-08-29T01:12:14Z", "losing-worker", "rev-b"),
                _event("2026-08-29T03:19:26Z", "lumina", "move", column="ready"),
            ],
            "owned-ready",
        ),
        (
            "6c418ad3",
            [],
            [
                _claim("2026-08-29T10:26:54Z", "worker", "rev-a"),
                _event("2026-08-29T10:28:23Z", "worker", "move", column="review"),
                _release("2026-08-29T11:22:36Z", "lumina", "worker", "rev-a"),
            ],
            "claimable",
        ),
        (
            "79396786",
            [],
            [
                _claim("2026-08-28T03:09:17Z", "worker", "rev-a"),
                _release("2026-08-28T03:13:00Z", "worker", "worker", "rev-a"),
                _event("2026-08-28T03:13:10Z", "worker", "move", column="review"),
                _event("2026-08-28T03:39:08Z", "reconcile", "assign", owner="worker"),
            ],
            "owned-review",
        ),
        (
            "87f90ae0",
            ["do-not-claim"],
            [
                _claim("2026-08-28T09:43:25Z", "worker", "rev-a"),
                _event("2026-08-28T09:45:09Z", "worker", "move", column="backlog"),
            ],
            "not-claimable",
        ),
        (
            "b6eedf67",
            [],
            [
                _claim("2026-08-29T10:27:36Z", "worker", "rev-a"),
                _event("2026-08-29T10:32:16Z", "worker", "move", column="review"),
                _release("2026-08-29T11:22:44Z", "lumina", "worker", "rev-a"),
            ],
            "claimable",
        ),
        (
            "dd659b4c",
            ["do-not-claim"],
            [
                _claim("2026-08-28T14:27:23Z", "worker", "rev-a"),
                _event("2026-08-28T14:29:48Z", "worker", "move", column="backlog"),
            ],
            "not-claimable",
        ),
        (
            "ff77ffb4",
            [],
            [
                _claim("2026-08-29T03:27:27Z", "worker", "rev-a"),
                _event("2026-08-29T03:29:48Z", "worker", "move", column="backlog"),
            ],
            "claimable",
        ),
    ],
)
def test_observed_ghost_ids_match_board_claimability(
    card_id: str,
    labels: list[str],
    events: list[dict[str, object]],
    expected: str,
) -> None:
    namespace = _load_claimability()
    core = _core(card_id, labels=labels)
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert namespace["_claimability_reason"](core, state) == expected


def test_move_does_not_release_owner_but_exact_release_after_move_does() -> None:
    namespace = _load_claimability()
    core = _core("move0001")
    claimed = _claim("2026-08-29T10:00:00Z", "worker", "rev-a")
    moved = _event("2026-08-29T10:01:00Z", "other", "move", column="backlog")

    state = namespace["_fold_claimability"](core, [moved, claimed])
    assert state["owner"] == "worker"
    assert state["status"] == "backlog"
    assert namespace["_claimability_reason"](core, state) == "claimable"

    released = _release("2026-08-29T10:02:00Z", "other", "worker", "rev-a")
    state = namespace["_fold_claimability"](core, [released, moved, claimed])
    assert state["owner"] is None
    assert state["status"] == "backlog"


def test_cross_writer_timestamp_order_and_stale_projection_parity() -> None:
    namespace = _load_claimability()
    core = _core("order001")
    events = [
        _event("2026-08-29T10:02:00Z", "projection", "move", column="review"),
        _claim("2026-08-29T10:01:00Z", "authoritative", "rev-a"),
        _event("2026-08-29T10:00:00Z", "legacy", "assign", owner="stale-owner"),
        _event("2026-08-29T10:03:00Z", "legacy", "link", owner="link-owner"),
    ]
    state = namespace["_fold_claimability"](core, events)
    assert state["owner"] == "authoritative"
    assert state["status"] == "review"
    assert namespace["_claimability_reason"](core, state) == "owned-review"


def test_terminal_review_dependency_gate_and_host_pin_reasons() -> None:
    namespace = _load_claimability()
    core = _core("states01")

    completed = namespace["_fold_claimability"](
        core,
        [_event("2026-08-29T10:00:00Z", "worker", "complete")],
    )
    assert namespace["_claimability_reason"](core, completed) == "done"

    archived = namespace["_fold_claimability"](
        core,
        [_event("2026-08-29T10:00:00Z", "worker", "archive")],
    )
    assert namespace["_claimability_reason"](core, archived) == "archive"

    voided = namespace["_fold_claimability"](
        core,
        [_event("2026-08-29T10:00:00Z", "worker", "void")],
    )
    assert namespace["_claimability_reason"](core, voided) == "void"

    review = namespace["_fold_claimability"](
        core,
        [_event("2026-08-29T10:00:00Z", "worker", "move", column="review")],
    )
    assert namespace["_claimability_reason"](core, review) == "claimable"

    dependent_core = {**core, "dependencies": ["missing-dep"]}
    dependent = namespace["_fold_claimability"](dependent_core, [])
    namespace["_dep_satisfied"] = lambda _dep: False
    assert namespace["_claimability_reason"](dependent_core, dependent) == "dependency"

    namespace["_dep_satisfied"] = lambda _dep: True
    namespace["host_pin"] = lambda _core, _labels: "chiap08"
    assert namespace["_claimability_reason"](core, review) == "host-pin:chiap08"


def test_pool_and_preclaim_call_the_same_predicate() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert "decision=authoritative_claimability(cid,core)" in source
    assert "fresh_claimability=authoritative_claimability(cid,fresh=True)" in source
    assert source.index("if blocked_backoff(cid):") < source.index(
        "decision=authoritative_claimability(cid,core)"
    )
    assert "CLAIMABILITY_EXCLUDED|" in source
    assert '"do-not-claim"' in source


def test_sensitive_category_requires_explicit_dispatch_approval() -> None:
    namespace = _load_claimability()
    core = {**_core("guard001"), "title": "Deploy production candidate"}
    state = namespace["_fold_claimability"](core, [])
    assert namespace["_claimability_reason"](core, state) == "sensitive-category"

    approved = {**core, "initial_labels": ["dispatch-approved"]}
    state = namespace["_fold_claimability"](approved, [])
    assert namespace["_claimability_reason"](approved, state) == "claimable"


def test_refreshed_description_criteria_and_review_links_are_folded() -> None:
    namespace = _load_claimability()
    core = {
        **_core("review01", labels=["review"]),
        "description": "stale description",
        "acceptance_criteria": ["stale criterion"],
    }
    digest = "a" * 64
    events = [
        _event(
            "2026-09-03T01:00:00Z",
            "jarvis",
            "describe",
            description="refreshed description",
        ),
        _event(
            "2026-09-03T01:01:00Z",
            "jarvis",
            "amend_criteria",
            criteria=["refreshed criterion"],
        ),
        _event(
            "2026-09-03T01:02:00Z",
            "jarvis",
            "link",
            link_key="producer_identity",
            link_value="producer-new",
        ),
        _event(
            "2026-09-03T01:03:00Z",
            "jarvis",
            "link",
            link_key="candidate_evidence_sha256",
            link_value=digest,
        ),
    ]

    state = namespace["_fold_claimability"](core, list(reversed(events)))

    assert state["description"] == "refreshed description"
    assert state["acceptance_criteria"] == ["refreshed criterion"]
    assert state["links"] == {
        "producer_identity": "producer-new",
        "candidate_evidence_sha256": digest,
    }


def test_malformed_lifecycle_fails_closed_with_reason() -> None:
    namespace = _load_claimability()

    def malformed(_card_id: str, fresh: bool = False) -> list[dict[str, object]]:
        del fresh
        raise ValueError("malformed event")

    namespace["_strict_card_events"] = malformed
    decision = namespace["authoritative_claimability"](
        "bad00001",
        core=_core("bad00001"),
        fresh=True,
    )
    assert decision == {"claimable": False, "reason": "malformed:ValueError"}


@pytest.mark.parametrize(
    "core_id",
    [pytest.param("missing", id="missing"), None, 123, "different"],
)
def test_malformed_core_identity_fails_closed_in_pool_and_preclaim(
    core_id: object,
    tmp_path: Path,
) -> None:
    namespace = _load_claimability()
    card_id = "identity"
    core = _core(card_id)
    if core_id == "missing":
        del core["id"]
    else:
        core["id"] = core_id

    expected = {"claimable": False, "reason": "malformed:ValueError"}
    assert namespace["authoritative_claimability"](card_id, core=core) == expected

    card_dir = tmp_path / card_id
    card_dir.mkdir()
    (card_dir / "core.json").write_text(json.dumps(core), encoding="utf-8")
    namespace["CARDS"] = str(tmp_path)
    assert namespace["authoritative_claimability"](card_id, fresh=True) == expected


def test_late_claim_after_complete_does_not_resurrect_56f9d32f() -> None:
    """Exact 56f9d32f stream: complete, late claim, release, late claim, release."""
    namespace = _load_claimability()
    core = _core("56f9d32f")
    events = [
        _claim("2026-09-04T09:56:00Z", "pi-codex-chiap04-56f9d32f", "b52e11d2"),
        _event(
            "2026-09-04T21:26:12Z",
            "jarvis",
            "release_claim",
            released_owner="pi-codex-chiap04-56f9d32f",
            expected_claim_revision="b52e11d2",
        ),
        _event("2026-09-04T21:26:13Z", "jarvis", "complete"),
        _claim("2026-09-04T21:29:11Z", "pi-codex-chiap04-56f9d32f", "21ed5df5"),
        _release(
            "2026-09-04T22:25:28Z",
            "pi-codex-chiap04-56f9d32f",
            "pi-codex-chiap04-56f9d32f",
            "21ed5df5",
        ),
        _claim("2026-09-04T22:29:11Z", "pi-codex-chiap04-56f9d32f", "4c04a452"),
        _release(
            "2026-09-04T22:30:18Z",
            "pi-codex-chiap04-56f9d32f",
            "pi-codex-chiap04-56f9d32f",
            "4c04a452",
        ),
    ]
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert state["status"] == "done"
    assert state["owner"] is None
    assert state["claim_revision"] is None
    assert namespace["_claimability_reason"](core, state) == "done"


def test_release_after_complete_keeps_status_done() -> None:
    """A zombie worker's matching release must not fold a done card to backlog."""
    namespace = _load_claimability()
    core = _core("relv0001")
    events = [
        _claim("2026-09-04T21:29:11Z", "worker", "rev-a"),
        _event("2026-09-04T21:26:13Z", "coordinator", "complete"),
    ]
    state = namespace["_fold_claimability"](core, events)
    assert state["status"] == "done"
    # The late claim was ignored, so owner is already None and a later
    # release with a stale owner does not match; feed a hypothetical stream
    # where the claim DID precede the complete instead.
    events2 = [
        _claim("2026-09-04T20:00:00Z", "worker", "rev-a"),
        _event("2026-09-04T20:30:00Z", "coordinator", "complete"),
        _release("2026-09-04T20:31:00Z", "worker", "worker", "rev-a"),
    ]
    state2 = namespace["_fold_claimability"](core, list(reversed(events2)))
    assert state2["status"] == "done"
    assert state2["owner"] is None
    assert state2["claim_revision"] is None
    assert namespace["_claimability_reason"](core, state2) == "done"


def test_void_is_sticky_against_late_claim_and_release() -> None:
    namespace = _load_claimability()
    core = _core("void0001")
    events = [
        _event("2026-09-04T10:00:00Z", "coordinator", "void"),
        _claim("2026-09-04T10:05:00Z", "worker", "rev-a"),
    ]
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert state["voided"] is True
    assert state["owner"] is None
    assert state["status"] == "backlog"
    assert namespace["_claimability_reason"](core, state) == "void"


def test_reopen_clears_terminal_stickiness() -> None:
    """Explicit reopen is the one sanctioned revival path."""
    namespace = _load_claimability()
    core = _core("reopen001")
    events = [
        _claim("2026-09-04T09:00:00Z", "worker", "rev-a"),
        _event("2026-09-04T10:00:00Z", "coordinator", "complete"),
        _event("2026-09-04T11:00:00Z", "coordinator", "reopen", column="ready"),
        _claim("2026-09-04T12:00:00Z", "worker2", "rev-b"),
    ]
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert state["status"] == "doing"
    assert state["owner"] == "worker2"
    assert namespace["_claimability_reason"](core, state) == "owned-doing"


def test_historical_4d98b588_stream_stays_done() -> None:
    """claim, move, claim, complete, assign, unassign, claim -> done."""
    namespace = _load_claimability()
    core = _core("4d98b588")
    events = [
        _claim("2026-08-28T01:00:00Z", "worker", "rev-a"),
        _event("2026-08-28T02:00:00Z", "worker", "move", column="doing"),
        _claim("2026-08-28T03:00:00Z", "worker", "rev-b"),
        _event("2026-08-28T04:00:00Z", "coordinator", "complete"),
        _event("2026-08-28T05:00:00Z", "coordinator", "assign", owner="reviewer"),
        _event("2026-08-28T06:00:00Z", "coordinator", "unassign"),
        _claim("2026-08-28T07:00:00Z", "worker", "rev-c"),
    ]
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert state["status"] == "done"
    assert namespace["_claimability_reason"](core, state) == "done"


def test_historical_92bd87a3_stream_stays_done() -> None:
    """claim, complete, assign, unassign -> done."""
    namespace = _load_claimability()
    core = _core("92bd87a3")
    events = [
        _claim("2026-08-28T01:00:00Z", "worker", "rev-a"),
        _event("2026-08-28T02:00:00Z", "coordinator", "complete"),
        _event("2026-08-28T03:00:00Z", "coordinator", "assign", owner="reviewer"),
        _event("2026-08-28T04:00:00Z", "coordinator", "unassign"),
    ]
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert state["status"] == "done"
    assert namespace["_claimability_reason"](core, state) == "done"


def test_cross_host_completion_race_resolves_terminal() -> None:
    """A complete written by another node wins over a later local claim view."""
    namespace = _load_claimability()
    core = _core("race0001")
    events = [
        _event("2026-09-04T21:26:13Z", "jarvis@chiap08", "complete"),
        _claim("2026-09-04T21:29:11Z", "pi-codex-chiap04", "rev-a"),
        _release(
            "2026-09-04T21:30:00Z",
            "pi-codex-chiap04",
            "pi-codex-chiap04",
            "rev-a",
        ),
    ]
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert state["status"] == "done"
    assert state["owner"] is None
    assert namespace["_claimability_reason"](core, state) == "done"


def test_unreleased_live_claim_still_folds_doing() -> None:
    """Guard does not change normal in-flight claim semantics."""
    namespace = _load_claimability()
    core = _core("alive001")
    events = [
        _event("2026-09-04T10:00:00Z", "coordinator", "move", column="ready"),
        _claim("2026-09-04T10:01:00Z", "worker", "rev-a"),
    ]
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert state["status"] == "doing"
    assert state["owner"] == "worker"
    assert namespace["_claimability_reason"](core, state) == "owned-doing"
