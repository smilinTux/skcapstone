"""Regression tests for the fleet launcher's authoritative claimability fold."""

from __future__ import annotations

import ast
import collections
import hashlib
import json
import os
import re
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.review_admission import governed_review_seat, qualified_reviewer_seats
from skcapstone.coordination import AgentFile, Board

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_claimability() -> dict[str, object]:
    """Load the dependency-free fold without executing the launcher."""
    names = {
        "_coord_task_claimable",
        "_dependency_value",
        "_fold_claimability",
        "_complete_governed_review",
        "_claimability_reason",
        "_authoritative_card_snapshot",
        "_authoritative_card_state",
        "authoritative_claimability",
        "_governed_review_metadata",
        "_pool_v2_admission",
        "_pool_v2_authority_rows",
        "_pool_v2_candidate_allowed",
        "_pool_v2_dispatchable",
        "_pool_v2_ready_ids",
        "_pool_v2_fingerprint",
        "_pool_v2_preclaim_matches",
        "_legacy_projection_owners",
        "_legacy_selector_decision",
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
        "hashlib": hashlib,
        "re": re,
        "non_implementation": lambda core, labels: (
            "[HUMAN]" in str(core.get("title") or "").upper() or "human-gate" in labels
        ),
        "os": os,
        "_legacy_claimability_events": lambda fresh=False: {},
        "_strict_card_events": lambda _cid, fresh=False: [],
        "_ONLY_SEAT": "",
        "_pool_v2_overlay": lambda _cid, _core, reason: {
            "reason": reason,
            "backoff": False,
        },
        "governed_review_seat": governed_review_seat,
        "qualified_reviewer_seats": qualified_reviewer_seats,
    }
    module = ast.Module(body=[nodes[name] for name in names], type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    namespace["_legacy_projection_owners"] = lambda _cid, fresh=False: ()
    return namespace


def test_legacy_only_owner_is_excluded_and_cleared_owner_reenters_pool() -> None:
    """A legacy claim blocks admission before route preflight; clearing it restores work."""
    namespace = _load_claimability()
    core = _core("f16c182c")
    namespace["_legacy_projection_owners"] = lambda _cid, fresh=False: ("jarvis",)
    owned = namespace["authoritative_claimability"]("f16c182c", core)
    assert owned["claimable"] is False
    assert owned["reason"] == "legacy-owned"
    assert (
        namespace["_pool_v2_dispatchable"](
            namespace["_pool_v2_admission"]("f16c182c", core, owned)
        )
        is False
    )

    namespace["_legacy_projection_owners"] = lambda _cid, fresh=False: ()
    cleared = namespace["authoritative_claimability"]("f16c182c", core, fresh=True)
    assert cleared["claimable"] is True
    assert cleared["reason"] == "claimable"


def test_conflicting_legacy_and_native_claim_fails_closed() -> None:
    """Disagreeing owner evidence is reported for reconciliation."""
    namespace = _load_claimability()
    core = _core("c26a1015")
    namespace["_strict_card_events"] = lambda _cid, fresh=False: [
        _claim("2026-09-16T10:00:00Z", "native", "rev-a")
    ]
    namespace["_legacy_projection_owners"] = lambda _cid, fresh=False: ("jarvis",)
    decision = namespace["authoritative_claimability"]("c26a1015", core)
    assert decision["claimable"] is False
    assert decision["reason"].startswith("malformed:LegacyOwnerConflict")


def test_legacy_claim_arriving_after_selection_blocks_preclaim() -> None:
    """A newly projected claim changes the bounded admission before preflight."""
    namespace = _load_claimability()
    core = _core("f16c182c")
    selected = namespace["_pool_v2_admission"](
        "f16c182c", core, namespace["authoritative_claimability"]("f16c182c", core)
    )
    namespace["_legacy_projection_owners"] = lambda _cid, fresh=False: ("jarvis",)
    fresh = namespace["_pool_v2_admission"](
        "f16c182c",
        core,
        namespace["authoritative_claimability"]("f16c182c", core, fresh=True),
    )
    assert selected["source_revision"] != fresh["source_revision"]
    assert namespace["_pool_v2_preclaim_matches"](selected, fresh) is False


def test_natural_projection_read_tracks_claim_and_clear(tmp_path: Path) -> None:
    """One cycle reads the board projection and the next fresh cycle sees its clear."""
    board = Board(tmp_path / ".skcapstone")
    board.ensure_dirs()
    board.save_agent(AgentFile(agent="jarvis", claimed_tasks=["f16c182c"]))
    namespace = _load_claimability()
    namespace.update(
        Board=Board,
        Path=Path,
        HOME=str(tmp_path),
        collections=collections,
        _legacy_projection_claims=None,
    )
    exec(
        compile(
            ast.Module(
                body=[
                    next(
                        node
                        for node in ast.parse(ROTATE.read_text()).body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_legacy_projection_owners"
                    )
                ],
                type_ignores=[],
            ),
            str(ROTATE),
            "exec",
        ),
        namespace,
    )
    assert namespace["_legacy_projection_owners"]("f16c182c") == ("jarvis",)
    board.save_agent(AgentFile(agent="jarvis", claimed_tasks=[]))
    assert namespace["_legacy_projection_owners"]("f16c182c", fresh=True) == ()


def test_legacy_selector_and_pool_v2_agree_on_projection_owner(tmp_path: Path) -> None:
    """Both selectors withhold the same ready card before gateway work."""
    namespace = _load_claimability()
    core = _core("f16c182c")
    core_path = tmp_path / "core.json"
    core_path.write_text(json.dumps(core), encoding="utf-8")
    namespace.update(
        excluded=set(),
        _REVIEW_READBACK_BLOCKED=set(),
        unclaimable=lambda _cid: False,
        itil_terminal=lambda _cid: False,
        lifecycle_state=lambda _cid: "open",
        outcome_lifecycle_bucket=lambda _lifecycle, _review: "open",
        awaiting_review=lambda _cid: False,
        blocked_backoff=lambda _cid: False,
        terminal_review_verdict=lambda _cid, _core: False,
        _legacy_projection_owners=lambda _cid, fresh=False: ("jarvis",),
    )
    legacy = namespace["_legacy_selector_decision"]("f16c182c", core_path)
    decision = namespace["authoritative_claimability"]("f16c182c", core)
    admission = namespace["_pool_v2_admission"]("f16c182c", core, decision)
    assert legacy["reason"] == "legacy-owned"
    assert legacy["eligible"] is False
    assert namespace["_pool_v2_dispatchable"](admission) is False


def test_projection_cycle_held_cleared_then_native_claimed(tmp_path: Path) -> None:
    """A complete selector cycle with real board projections never dispatches owners."""
    board = Board(tmp_path / ".skcapstone")
    board.ensure_dirs()
    core = _core("f16c182c")
    core_path = tmp_path / "core.json"
    core_path.write_text(json.dumps(core), encoding="utf-8")
    namespace = _load_claimability()
    namespace.update(
        Board=Board,
        Path=Path,
        HOME=str(tmp_path),
        collections=collections,
        _legacy_projection_claims=None,
        excluded=set(),
        _REVIEW_READBACK_BLOCKED=set(),
        unclaimable=lambda _cid: False,
        itil_terminal=lambda _cid: False,
        lifecycle_state=lambda _cid: "open",
        outcome_lifecycle_bucket=lambda _lifecycle, _review: "open",
        awaiting_review=lambda _cid: False,
        blocked_backoff=lambda _cid: False,
        terminal_review_verdict=lambda _cid, _core: False,
    )
    exec(
        compile(
            ast.Module(
                body=[
                    next(
                        node
                        for node in ast.parse(ROTATE.read_text()).body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_legacy_projection_owners"
                    )
                ],
                type_ignores=[],
            ),
            str(ROTATE),
            "exec",
        ),
        namespace,
    )

    namespace["_strict_card_events"] = lambda _cid, fresh=False: [
        _claim("2026-09-15T10:00:00Z", "jarvis", "rev-old"),
        _release("2026-09-15T11:00:00Z", "jarvis", "jarvis", "rev-old"),
    ]
    board.save_agent(
        AgentFile(
            agent="jarvis",
            last_seen="2020-01-01T00:00:00Z",
            claimed_tasks=["f16c182c"],
        )
    )
    held = namespace["_legacy_selector_decision"]("f16c182c", core_path)
    assert held["reason"] == "legacy-owned"
    assert not held["eligible"]
    assert not namespace["_pool_v2_dispatchable"](
        namespace["_pool_v2_admission"]("f16c182c", core, held["decision"])
    )

    board.save_agent(AgentFile(agent="jarvis", claimed_tasks=[]))
    namespace["_legacy_projection_owners"]("f16c182c", fresh=True)
    cleared = namespace["_legacy_selector_decision"]("f16c182c", core_path)
    assert cleared["eligible"]
    assert namespace["_pool_v2_dispatchable"](
        namespace["_pool_v2_admission"]("f16c182c", core, cleared["decision"])
    )

    namespace["_strict_card_events"] = lambda _cid, fresh=False: [
        _claim("2026-09-16T10:00:00Z", "native", "rev-a")
    ]
    native = namespace["_legacy_selector_decision"]("f16c182c", core_path)
    assert not native["eligible"]
    assert native["reason"].startswith("owned-")


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
            "review",
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
            "review",
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


def test_source_bindings_fold_from_normal_link_events() -> None:
    namespace = _load_claimability()
    core = _core("source01", labels=["source-only"])
    events = [
        _event(
            "2026-09-08T22:00:00Z",
            "jarvis",
            "link",
            link_key="repository",
            link_value="https://github.com/smilinTux/sklegal",
        ),
        _event(
            "2026-09-08T22:00:01Z",
            "jarvis",
            "link",
            link_key="base_ref",
            link_value="main",
        ),
    ]
    state = namespace["_fold_claimability"](core, events)
    assert state["links"] == {
        "repository": "https://github.com/smilinTux/sklegal",
        "base_ref": "main",
    }
    assert not any(state["review_markers"].values())


@pytest.mark.parametrize(
    "key,value",
    [("repository", ""), ("base_ref", "   "), ("base_revision", "")],
)
def test_empty_source_binding_link_event_fails_closed(key: str, value: str) -> None:
    namespace = _load_claimability()
    core = _core("source02", labels=["source-only"])
    event = _event(
        "2026-09-08T22:00:00Z",
        "jarvis",
        "link",
        link_key=key,
        link_value=value,
    )
    with pytest.raises(ValueError, match="typed review metadata is malformed"):
        namespace["_fold_claimability"](core, [event])


@pytest.mark.parametrize(
    ("card_id", "key"),
    [
        ("7ddb7d1e", "pr"),
        ("835a7e9d", "pr"),
        ("a830be09", "evidence_sha256"),
        ("bdf2774a", "pr"),
        ("fd16ac85", "pr"),
    ],
)
def test_empty_optional_historical_review_links_are_ignored(card_id: str, key: str) -> None:
    namespace = _load_claimability()
    core = _core(card_id)
    event = _event(
        "2026-09-08T22:00:00Z",
        "jarvis",
        "link",
        link_key=key,
        link_value="",
    )

    state = namespace["_fold_claimability"](core, [event])

    assert key not in state["links"]


@pytest.mark.parametrize("key", ["pr", "evidence", "evidence_sha256"])
def test_each_empty_optional_review_link_is_ignored(key: str) -> None:
    namespace = _load_claimability()
    event = _event(
        "2026-09-08T22:00:00Z",
        "jarvis",
        "link",
        link_key=key,
        link_value="   ",
    )

    state = namespace["_fold_claimability"](_core("optional"), [event])

    assert key not in state["links"]


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
    assert namespace["_claimability_reason"](core, review) == "review"

    dependent_core = {**core, "dependencies": ["missing-dep"]}
    dependent = namespace["_fold_claimability"](dependent_core, [])
    namespace["_dep_satisfied"] = lambda _dep: False
    assert namespace["_claimability_reason"](dependent_core, dependent) == "dependency"

    namespace["_dep_satisfied"] = lambda _dep: True
    namespace["host_pin"] = lambda _core, _labels: "chiap08"
    assert namespace["_claimability_reason"](core, review) == "host-pin:chiap08"


def test_governed_reviewer_reaches_assignment_without_admitting_source_cards() -> None:
    namespace = _load_claimability()
    core = {
        **_core("feedbeef", labels=["review"]),
        "title": "[REVIEW] Independently evaluate candidate",
        "links": {"producer_identity": "producer", "candidate_evidence_sha256": "a" * 64},
    }
    state = namespace["_fold_claimability"](core, [])
    assert namespace["_claimability_reason"](core, state) == "review"
    for links in (
        {"open_pr": "https://example.invalid/pr/1"},
        {"candidate_evidence_sha256": "a" * 64},
    ):
        source = {**core, "links": links, "description": "PASS_FOR_REVIEW source candidate"}
        folded = namespace["_fold_claimability"](source, [])
        assert namespace["_claimability_reason"](source, folded) == "review"


def test_review_markers_are_not_executable_after_claim_release() -> None:
    namespace = _load_claimability()
    for labels, description in ((["review"], "ordinary"), ([], "PASS_FOR_REVIEW evidence exists")):
        core = {**_core("review-marker", labels=labels), "description": description}
        state = namespace["_fold_claimability"](core, [])
        assert namespace["_claimability_reason"](core, state) == "review"


def test_released_64c201a1_review_stays_withheld_until_explicit_move_review() -> None:
    """A release cannot manufacture governed review lifecycle authority."""
    namespace = _load_claimability()
    core = {
        **_core("64c201a1", labels=["review", "seat-seraph", "sk-s"]),
        "title": "[REVIEW][S] Verify candidate",
        "links": {
            "producer_identity": "producer",
            "candidate_evidence_sha256": "a" * 64,
            "link_source_card": "source01",
            "link_head_revision": "b" * 40,
        },
    }
    events = [
        _claim("2026-09-13T01:00:00Z", "reviewer", "revision-1"),
        _event("2026-09-13T01:01:00Z", "reviewer", "move", column="doing"),
        _event("2026-09-13T01:02:00Z", "reviewer", "describe", title=""),
        _release("2026-09-13T01:03:00Z", "reviewer", "reviewer", "revision-1"),
    ]

    state = namespace["_fold_claimability"](core, events)
    reason = namespace["_claimability_reason"](core, state)
    state.update(
        claimable=reason in {"claimable", "governed-review"},
        reason=reason,
        core={**core, "title": state["title"], "links": state["links"]},
        source_revision="c" * 64,
        host_pin=None,
    )
    admission = namespace["_pool_v2_admission"]("64c201a1", core, state)

    assert state["status"] == "backlog"
    assert reason == "review"
    assert admission["governed_review"] is True
    assert admission["elastic_review_admitted"] is False

    reviewed = namespace["_fold_claimability"](
        core,
        [*events, _event("2026-09-13T01:04:00Z", "operator", "move", column="review")],
    )
    reviewed_reason = namespace["_claimability_reason"](core, reviewed)
    reviewed.update(
        claimable=reviewed_reason in {"claimable", "governed-review"},
        reason=reviewed_reason,
        core={**core, "title": reviewed["title"], "links": reviewed["links"]},
        source_revision="e" * 64,
        host_pin=None,
    )
    assert reviewed_reason == "governed-review"
    assert (
        namespace["_pool_v2_admission"]("64c201a1", core, reviewed)["elastic_review_admitted"]
        is True
    )

    paused = namespace["_fold_claimability"](
        core,
        [*events, _event("2026-09-13T01:04:00Z", "operator", "move", column="backlog")],
    )
    paused.update(
        claimable=False,
        reason=namespace["_claimability_reason"](core, paused),
        core={**core, "title": paused["title"], "links": paused["links"]},
        source_revision="d" * 64,
        host_pin=None,
    )
    assert (
        namespace["_pool_v2_admission"]("64c201a1", core, paused)["elastic_review_admitted"]
        is False
    )


@pytest.mark.parametrize("column", ["backlog", "ready", "doing"])
def test_release_claim_preserves_nonreview_column(column: str) -> None:
    """Ownership release never changes lifecycle without an explicit move."""
    namespace = _load_claimability()
    core = {
        **_core("deadbeef", labels=["review", "seat-seraph", "sk-s"]),
        "links": {
            "producer_identity": "producer",
            "candidate_evidence_sha256": "a" * 64,
            "link_source_card": "source01",
            "link_head_revision": "b" * 40,
        },
    }
    events = []
    if column != "backlog":
        events.append(_event("2026-09-15T01:00:00Z", "owner", "move", column=column))
    events.extend(
        [
            _claim("2026-09-15T01:01:00Z", "owner", "revision-1"),
            _release("2026-09-15T01:02:00Z", "owner", "owner", "revision-1"),
        ]
    )

    state = namespace["_fold_claimability"](core, events)

    assert state["status"] == column
    assert namespace["_claimability_reason"](core, state) == "review"


def test_claim_origin_status_is_captured_once_per_claim_generation() -> None:
    """Owned moves cannot replace either generation's pre-claim column."""
    namespace = _load_claimability()
    core = _core("deadbeef")
    events = [
        _claim("2026-09-15T01:00:00Z", "owner", "revision-1"),
        _event("2026-09-15T01:01:00Z", "owner", "move", column="ready"),
        _event("2026-09-15T01:02:00Z", "owner", "move", column="review"),
        _release("2026-09-15T01:03:00Z", "owner", "owner", "revision-1"),
        _event("2026-09-15T01:04:00Z", "operator", "move", column="ready"),
        _claim("2026-09-15T01:05:00Z", "owner", "revision-2"),
        _event("2026-09-15T01:06:00Z", "owner", "move", column="doing"),
        _release("2026-09-15T01:07:00Z", "owner", "owner", "revision-2"),
    ]

    state = namespace["_fold_claimability"](core, events)

    assert state["status"] == "ready"
    assert state["claim_origin_status"] is None


def test_a6a2f0f9_owned_moves_do_not_replace_preclaim_backlog() -> None:
    """The exact live lifecycle is withheld before bounded selection."""
    namespace = _load_claimability()
    core = {
        **_core("a6a2f0f9", labels=["sklegal", "review", "source-only", "sk-s", "seat-seraph"]),
        "title": "[SKLEGAL][S][REVIEW] Verify migration",
        "links": {
            "producer_identity": "codex-3406cf8d",
            "candidate_evidence_sha256": "a" * 64,
            "link_source_card": "3406cf8d",
            "link_head_revision": "b" * 40,
        },
    }
    lifecycle = [
        _claim("2026-09-11T23:46:41Z", "seraph", "revision-1"),
        _event("2026-09-11T23:46:44Z", "seraph", "move", column="doing"),
        _event("2026-09-11T23:48:30Z", "seraph", "move", column="ready"),
        _event("2026-09-11T23:52:05Z", "seraph", "move", column="review"),
        _release("2026-09-12T01:13:58Z", "jarvis", "seraph", "revision-1"),
    ]
    stale = namespace["_fold_claimability"](core, lifecycle)
    stale_reason = namespace["_claimability_reason"](core, stale)
    stale.update(
        claimable=stale_reason in {"claimable", "governed-review"},
        reason=stale_reason,
        core={**core, "title": stale["title"], "links": stale["links"]},
        source_revision="c" * 64,
        host_pin=None,
    )
    stale_admission = namespace["_pool_v2_admission"]("a6a2f0f9", core, stale)
    valid_admission = {
        "card_id": "feedface",
        "claimable": True,
        "reason": "claimable",
        "governed_review": False,
        "host_pin": None,
        "title": "Later valid work",
        "labels": ["skcapstone", "sk-s"],
        "core": {"id": "feedface"},
        "overlay": {"backoff": False},
        "source_revision": "d" * 64,
    }
    decisions = [
        SimpleNamespace(card_id="a6a2f0f9", eligible=True),
        SimpleNamespace(card_id="feedface", eligible=True),
    ]

    ready = namespace["_pool_v2_ready_ids"](
        decisions,
        {"a6a2f0f9": stale_admission, "feedface": valid_admission},
    )
    rows, _pinned = namespace["_pool_v2_authority_rows"](
        decisions,
        {"a6a2f0f9": stale_admission, "feedface": valid_admission},
        False,
        {},
        {None: 4},
        (),
        "chiap03",
    )

    assert stale["status"] == "backlog"
    assert stale_reason == "sensitive-category"
    assert stale_admission["elastic_review_admitted"] is False
    assert ready == {"feedface"}
    assert [row[2] for row in rows[:1]] == ["feedface"]

    explicit_review = namespace["_fold_claimability"](
        core,
        [*lifecycle, _event("2026-09-12T01:14:00Z", "jarvis", "move", column="review")],
    )
    assert namespace["_claimability_reason"](core, explicit_review) == "governed-review"


@pytest.mark.parametrize(
    ("core_update", "events", "expected"),
    [
        ({"title": "[HUMAN] Review approval"}, [], "human-gate"),
        ({"dependencies": ["blocked-dependency"]}, [], "dependency"),
        ({"title": "[REVIEW] Production deployment"}, [], "sensitive-category"),
        (
            {},
            [_claim("2026-09-07T10:00:00Z", "other-reviewer", "revision-1")],
            "owned-doing",
        ),
    ],
)
def test_review_marker_never_overrides_safety_exclusions(
    core_update: dict[str, object],
    events: list[dict[str, object]],
    expected: str,
) -> None:
    """Review routing happens only after every ordinary exclusion."""

    namespace = _load_claimability()
    namespace["_dep_satisfied"] = lambda _dep: False
    core = {
        **_core("review-safety", labels=["review"]),
        "links": {
            "producer_identity": "producer",
            "candidate_evidence_sha256": "a" * 64,
        },
        **core_update,
    }
    state = namespace["_fold_claimability"](core, events)

    assert namespace["_claimability_reason"](core, state) == expected


def test_review_evidence_and_open_pr_links_are_folded_and_excluded() -> None:
    namespace = _load_claimability()
    for links in (
        {"open_pr": "https://example.invalid/pr/1"},
        {"candidate_evidence_sha256": "a" * 64},
    ):
        core = {**_core("review-link", labels=[]), "links": links}
        state = namespace["_fold_claimability"](core, [])
        assert state["links"] == links
        assert namespace["_claimability_reason"](core, state) == "review"


def test_pool_and_preclaim_call_the_same_predicate() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert "decision=authoritative_claimability(cid,core)" in source
    assert (
        "fresh_claimability=authoritative_claimability(" "cid,core=_fresh_core,fresh=True)"
    ) in source
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


def test_legacy_pool_input_preserves_raw_core_revision_after_criteria_amendment(
    tmp_path: Path,
) -> None:
    """The real legacy selection path must agree with a fresh raw preclaim."""
    namespace = _load_claimability()
    card_id = "cdf59956"
    raw_core = {
        **_core(card_id),
        "acceptance_criteria": ["original criterion"],
    }
    core_path = tmp_path / "core.json"
    core_path.write_text(json.dumps(raw_core), encoding="utf-8")
    events = [
        _event(
            "2026-09-16T01:00:00Z",
            "dev208",
            "amend_criteria",
            criteria=["amended criterion"],
        )
    ]
    namespace["_strict_card_events"] = lambda _cid, fresh=False: list(events)
    namespace.update(
        excluded=set(),
        _REVIEW_READBACK_BLOCKED=set(),
        unclaimable=lambda _cid: False,
        itil_terminal=lambda _cid: False,
        lifecycle_state=lambda _cid: "open",
        outcome_lifecycle_bucket=lambda _state, _review: "open",
        awaiting_review=lambda _cid: False,
        blocked_backoff=lambda _cid: False,
        terminal_review_verdict=lambda _cid, _core: False,
    )
    source = ROTATE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    selector = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_legacy_selector_decision"
    )
    exec(compile(ast.Module(body=[selector], type_ignores=[]), str(ROTATE), "exec"), namespace)
    legacy = namespace["_legacy_selector_decision"](card_id, str(core_path))
    assert legacy["eligible"] is True
    assert legacy["core"] == raw_core
    assert legacy["decision"]["core"]["acceptance_criteria"] == ["amended criterion"]

    selection = source.split('    core=legacy["core"]', 1)[1].split("# How many OTHER cards", 1)[0]
    namespace.update(
        cid=card_id,
        legacy=legacy,
        HOST="chiap03",
        _PINNED_IDS=set(),
        ENG=(),
        PRI={"None": 4},
        pool=[],
        _pool_v2_inputs=[],
        _pool_v2_input_ids=set(),
    )
    exec('core=legacy["core"]\n' + textwrap.dedent(selection), namespace)
    assert namespace["pool"][0][3]["acceptance_criteria"] == ["amended criterion"]
    selected_core = namespace["_pool_v2_inputs"][0][1]
    selected = namespace["authoritative_claimability"](card_id, selected_core)
    fresh = namespace["authoritative_claimability"](card_id, raw_core, fresh=True)
    assert selected["source_revision"] == fresh["source_revision"]
    assert selected["core"] == fresh["core"]
    selected_admission = namespace["_pool_v2_admission"](card_id, selected_core, selected)
    fresh_admission = namespace["_pool_v2_admission"](card_id, raw_core, fresh)
    assert namespace["_pool_v2_preclaim_matches"](selected_admission, fresh_admission)

    changed = {**raw_core, "acceptance_criteria": ["external change"]}
    changed_fresh = namespace["authoritative_claimability"](card_id, changed, fresh=True)
    assert selected["source_revision"] != changed_fresh["source_revision"]
    changed_admission = namespace["_pool_v2_admission"](card_id, changed, changed_fresh)
    assert not namespace["_pool_v2_preclaim_matches"](selected_admission, changed_admission)


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


@pytest.mark.parametrize("action", ["move", "reopen"])
def test_explicit_return_to_ready_clears_review_history(action: str) -> None:
    namespace = _load_claimability()
    core = _core("feedbeef")
    events = [
        _claim("2026-09-04T09:00:00Z", "worker", "rev-a"),
        _event("2026-09-04T10:00:00Z", "worker", "move", column="review"),
        _release("2026-09-04T11:00:00Z", "worker", "worker", "rev-a"),
    ]
    folded = namespace["_fold_claimability"](core, events)
    assert namespace["_claimability_reason"](core, folded) == "review"
    events.append(_event("2026-09-04T12:00:00Z", "coordinator", action, column="ready"))
    folded = namespace["_fold_claimability"](core, events)
    assert namespace["_claimability_reason"](core, folded) == "claimable"


@pytest.mark.parametrize("action", ["move", "reopen"])
@pytest.mark.parametrize("marker", ["evidence_sha256", "pr", "title", "description"])
def test_explicit_executable_transition_preserves_but_supersedes_review_markers(action, marker):
    namespace = _load_claimability()
    core = _core("feedbeef")
    if marker in {"evidence_sha256", "pr"}:
        core["links"] = {marker: "historical-value"}
        new_marker = dict(action="link", link_key=marker, link_value="new-value")
    elif marker == "title":
        core["title"] = "[REVIEW] Historical candidate"
        new_marker = dict(action="describe", title="[REVIEW] New candidate")
    else:
        core["description"] = "PASS_FOR_REVIEW historical candidate"
        new_marker = dict(action="describe", description="PASS_FOR_REVIEW new candidate")
    prior = namespace["_fold_claimability"](core, [])
    assert namespace["_claimability_reason"](core, prior) == "review"
    events = [_event("2026-09-04T12:00:00Z", "coordinator", action, column="ready")]
    current = namespace["_fold_claimability"](core, events)
    assert namespace["_claimability_reason"](core, current) == "claimable"
    for key in ("links", "title", "description"):
        assert current[key] == prior[key]
    events.append(_event("2026-09-04T13:00:00Z", "worker", **new_marker))
    newer = namespace["_fold_claimability"](core, events)
    assert namespace["_claimability_reason"](core, newer) == "review"


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
