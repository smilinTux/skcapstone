"""Regression tests for the fleet launcher's authoritative claimability fold."""

from __future__ import annotations

import ast
import glob
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
        "_active_repair_parents",
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
        "_REPAIR_LABELS": {"repair", "source-repair"},
        "_PARENT_LABEL_RE": re.compile(r"^parent-([0-9a-f]{8})$", re.I),
        "_active_repair_parents_cache": None,
        "_dep_satisfied": lambda _dep: True,
        "host_pin": lambda _core, _labels: None,
        "glob": glob,
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


def test_parent_is_excluded_only_while_distinct_repair_is_actively_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _load_claimability()
    parent = "a1b2c3d4"
    repair = "b8eca7f1"
    cores = {
        parent: _core(parent),
        repair: _core(repair, labels=["repair", f"parent-{parent}"]),
    }
    states = {
        parent: namespace["_fold_claimability"](
            cores[parent],
            [
                _claim("2026-09-01T03:50:00Z", "old-parent-worker", "parent-rev"),
                _release(
                    "2026-09-01T03:51:00Z",
                    "old-parent-worker",
                    "old-parent-worker",
                    "parent-rev",
                ),
            ],
        ),
        repair: namespace["_fold_claimability"](
            cores[repair], [_claim("2026-09-01T03:53:12Z", "repair-worker", "repair-rev")]
        ),
    }
    namespace["_authoritative_card_state"] = (
        lambda card_id, core=None, fresh=False: (cores[card_id], states[card_id])
    )
    monkeypatch.setattr(
        namespace["glob"], "glob", lambda _pattern: [f"/cards/{parent}", f"/cards/{repair}"]
    )

    assert namespace["authoritative_claimability"](parent)["reason"] == "active-repair"

    states[repair] = namespace["_fold_claimability"](
        cores[repair],
        [
            _claim("2026-09-01T03:53:12Z", "repair-worker", "repair-rev"),
            _release(
                "2026-09-01T04:00:00Z", "repair-worker", "repair-worker", "repair-rev"
            ),
        ],
    )
    namespace["_active_repair_parents_cache"] = None
    assert namespace["authoritative_claimability"](parent)["reason"] == "claimable"

    states[repair] = namespace["_fold_claimability"](
        cores[repair],
        [
            _claim("2026-09-01T03:53:12Z", "repair-worker", "repair-rev"),
            _event("2026-09-01T04:00:00Z", "repair-worker", "complete"),
        ],
    )
    namespace["_active_repair_parents_cache"] = None
    assert namespace["authoritative_claimability"](parent)["reason"] == "claimable"


def test_repair_claim_fold_keeps_exact_owner_and_revision_across_worker_dimensions() -> None:
    namespace = _load_claimability()
    parent = "a1b2c3d4"
    repair = "b8eca7f1"
    core = _core(repair, labels=["source-repair", f"parent-{parent}"])
    events = [
        _claim("2026-09-01T03:53:10Z", "pi-qwen-chiap01-b8eca7f1", "qwen-rev"),
        _claim("2026-09-01T03:53:11Z", "pi-glm-chiap03-b8eca7f1", "glm-rev"),
        _release(
            "2026-09-01T03:53:12Z",
            "pi-glm-chiap03-b8eca7f1",
            "pi-qwen-chiap01-b8eca7f1",
            "wrong-rev",
        ),
    ]
    state = namespace["_fold_claimability"](core, list(reversed(events)))
    assert state["owner"] == "pi-qwen-chiap01-b8eca7f1"
    assert state["claim_revision"] == "qwen-rev"
    assert state["status"] == "doing"

    namespace["_authoritative_card_state"] = (
        lambda card_id, core=None, fresh=False: (core, state)
    )
    assert namespace["_active_repair_parents"]([repair], fresh=True) == {parent}


def test_sensitive_category_requires_explicit_dispatch_approval() -> None:
    namespace = _load_claimability()
    core = {**_core("guard001"), "title": "Deploy production candidate"}
    state = namespace["_fold_claimability"](core, [])
    assert namespace["_claimability_reason"](core, state) == "sensitive-category"

    approved = {**core, "initial_labels": ["dispatch-approved"]}
    state = namespace["_fold_claimability"](approved, [])
    assert namespace["_claimability_reason"](approved, state) == "claimable"


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
