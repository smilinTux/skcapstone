"""Builder routing fences and the authority occupancy-cap fall-through."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from skcapstone.fleet import builder_dispatch as bd

ROTATE = Path(__file__).resolve().parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"
CORE = {"id": "aaaaaaaa"}


@pytest.fixture(autouse=True)
def _no_production(monkeypatch):
    monkeypatch.delenv("SKFLEET_PRODUCTION_POLICY", raising=False)


@pytest.mark.parametrize(
    ("labels", "reason"),
    [
        (["source-only", "sk-m"], None),
        (["source-only"], "no-logical-route"),
        (["source-only", "sk-m", "sk-l"], "no-logical-route"),
        (["sk-m"], "not-source-work"),
        (["no-external-action", "sk-m"], "not-source-work"),
        (["source-only", "sk-m", "seat-niobe"], "seat-pinned"),
        (["source-only", "sk-m", "parked-ops"], "parked"),
        (["source-only", "sk-m", "host-pin"], "host-pin"),
        (["source-only", "sk-m", "do-not-claim"], "do-not-claim"),
        (["source-only", "sk-m", "glm-only"], "lane-only-label"),
    ],
)
def test_ineligibility_names_the_first_fence(labels, reason):
    assert bd.ineligibility(CORE, labels) == reason
    assert bd.eligible(CORE, labels) is (reason is None)


def test_production_glm_only_needs_dispatch_approved(monkeypatch):
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", "1")
    assert bd.ineligibility(CORE, ["glm-only", "sk-m"]) == "not-source-work"
    assert bd.ineligibility(CORE, ["glm-only", "dispatch-approved", "sk-m"]) is None
    assert bd.ineligibility(CORE, ["glm-only", "dispatch-approved", "sk-m", "seat-niobe"]) == (
        "seat-pinned"
    )


def test_invalid_card_id_is_ineligible():
    assert bd.ineligibility({"id": 7}, ["source-only", "sk-m"]) == "invalid-card"


def test_fallthrough_offers_skip_fenced_and_already_offered():
    rows = [
        ("11111111", {}, ["source-only", "sk-m"]),
        ("22222222", {}, ["source-only", "sk-m", "seat-niobe"]),
        ("33333333", {}, ["sk-m"]),
        ("44444444", {}, ["source-only", "sk-s"]),
        ("55555555", {}, ["source-only"]),
    ]
    offerable, skipped = bd.fallthrough_offers(rows, {"44444444"})
    assert [row[0] for row in offerable] == ["11111111"]
    assert skipped == {
        "seat-pinned": 1,
        "not-source-work": 1,
        "already-offered-this-cycle": 1,
        "no-logical-route": 1,
    }


def _helper():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_fallthrough_to_builders"
    )
    ns = {"builder_dispatch": bd}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(ROTATE), "exec"), ns)
    return ns["_fallthrough_to_builders"]


def test_cap_fallthrough_offers_remaining_cards_and_keeps_every_fence():
    helper = _helper()
    offered_ids, lines = [], []

    def offer(card_id, core, labels):
        offered_ids.append(card_id)
        if card_id == "bbbbbbbb":
            raise bd.BuilderDispatchError("required-test-profile-unqualified")
        if card_id == "cccccccc":
            return None
        return {"node": "node-chiap01", "request_id": "r" + card_id}

    rows = [
        ("aaaaaaaa", {}, ["source-only", "sk-m"]),
        ("bbbbbbbb", {}, ["source-only", "sk-m"]),
        ("cccccccc", {}, ["source-only", "sk-m"]),
        ("dddddddd", {}, ["source-only", "sk-m", "seat-niobe"]),
        ("eeeeeeee", {}, ["review", "seat-seraph"]),
    ]
    attempted: set[str] = set()
    offered, blocked, skipped = helper(rows, attempted, offer, lines.append, 100.0, lambda: 0.0)
    assert (offered, blocked) == (1, 1)
    assert offered_ids == ["aaaaaaaa", "bbbbbbbb", "cccccccc"]
    assert skipped == {"builder-idle": 1, "seat-pinned": 1, "no-logical-route": 1}
    assert attempted == {"aaaaaaaa", "bbbbbbbb", "cccccccc"}
    assert lines[0].startswith("BUILDER_FALLTHROUGH_OFFERED|aaaaaaaa|node=node-chiap01")
    assert lines[1].startswith("BUILDER_FALLTHROUGH_BLOCKED|bbbbbbbb")


def test_cap_fallthrough_stops_at_the_cycle_deadline():
    helper = _helper()
    calls = []
    rows = [("aaaaaaaa", {}, ["source-only", "sk-m"])]
    offered, _, skipped = helper(
        rows, set(), lambda *a: calls.append(a), lambda _l: None, 5.0, lambda: 9.0
    )
    assert (offered, calls, skipped) == (0, [], {"cycle-deadline": 1})


def test_cap_branch_falls_through_before_it_breaks():
    source = ROTATE.read_text(encoding="utf-8")
    cap = source.index('"LANE_LAUNCH_STOPPED|%s|reason=node-occupancy-cap"')
    tail = source[cap : cap + 1800]
    assert "_fallthrough_to_builders(" in tail
    assert "if not DRY and _is_niobe_builder_host(HOST):" in tail
    assert tail.index("_fallthrough_to_builders(") < tail.index("break")
    assert "_builder_attempted_ids.add(_candidate[2])" in source
