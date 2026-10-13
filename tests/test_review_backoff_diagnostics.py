"""Backoff diagnostics distinguish completed review work from blocked work."""

import ast
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore

from skcapstone.card_store import CardStore
from skcapstone.cli import main
from skcapstone.review_admission import governed_review_gate_reasons
from tests.test_coord_gate_diagnostic import _write_gateway_capacity


@pytest.mark.parametrize("verdict", ["PASS", "PASS - checked", "PASS_FOR_REVIEW"])
def test_gates_reports_pass_waiting_for_acceptance(tmp_path, monkeypatch, verdict):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _write_gateway_capacity(tmp_path)
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="ab000001",
            title="[S] review",
            initial_labels=["review", "seat-seraph"],
            meta={
                "producer_identity": "producer",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "ab000002",
                "link_head_revision": "b" * 40,
            },
        )
    )
    store.append_event("ab000001", "link", "reviewer", link_key="verdict", link_value=verdict)
    result = CliRunner().invoke(main, ["coord", "gates", "ab000001", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "backoff:pass-awaiting-acceptance" in json.loads(result.output)["reasons"]
    store.append_event("ab000001", "move", "operator", column="ready")
    result = CliRunner().invoke(main, ["coord", "gates", "ab000001", "--home", str(tmp_path)])
    assert not any(r.startswith("backoff:") for r in json.loads(result.output)["reasons"])


def test_dependency_hold_is_named_blocked_backoff():
    reasons = governed_review_gate_reasons({}, ["review"], dependency_blocker_holds=True)
    assert "backoff:blocked" in reasons
    assert "dependency-blocker" not in reasons


def test_histogram_uses_existing_overlay_backoff_without_changing_dispatch():
    path = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(path.read_text())
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.For)
        and isinstance(n.target, ast.Tuple)
        and [e.id for e in n.target.elts if isinstance(e, ast.Name)] == ["_cid", "_admission"]
    )
    cards = {}
    for i, overlay in enumerate(
        [
            {"backoff": True, "awaiting_review": True},
            {"backoff": True},
            {"backoff": False, "awaiting_review": True},
            {"backoff": True, "claim_ceiling": True},
        ]
    ):
        cards[str(i)] = {
            "labels": ["review", "seat-seraph"],
            "overlay": overlay,
            "core": {
                "meta": {
                    "producer_identity": "producer",
                    "candidate_evidence_sha256": "a" * 64,
                    "link_source_card": "ab000002",
                    "link_head_revision": "b" * 40,
                }
            },
        }
    ns = dict(
        _POOL_V2_ADMISSIONS=cards,
        governed_review_gate_reasons=governed_review_gate_reasons,
        _lane_deferred_cards=set(),
        _SEAT_BLOCKED=set(),
        _review_withheld=[],
        _pool_v2_dispatchable=lambda a: not a["overlay"]["backoff"],
    )
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
    assert ns["_review_withheld"] == [
        ("0", "backoff:pass-awaiting-acceptance"),
        ("1", "backoff:blocked"),
        ("3", "backoff:claim-ceiling"),
    ]


def test_coord_gates_names_unresolved_dependency_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _write_gateway_capacity(tmp_path)
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="ab000001",
            title="[S] review",
            initial_labels=["review", "seat-seraph"],
            meta={
                "producer_identity": "producer",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "ab000002",
                "link_head_revision": "b" * 40,
                "blocked_on": "dependency referent=card:ab000002",
                "evidence_sha256": "c" * 64,
            },
        )
    )
    result = CliRunner().invoke(main, ["coord", "gates", "ab000001", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    reasons = json.loads(result.output)["reasons"]
    assert "backoff:blocked" in reasons
    assert "dependency-blocker" not in reasons


@pytest.mark.parametrize("verified", [True, False])
def test_pass_clears_only_for_verified_remote_supersession(tmp_path, monkeypatch, verified):
    from types import SimpleNamespace

    from skcapstone.coord_gate_diagnostic import _awaiting_acceptance
    from skcapstone.fleet import review_retire

    store = CardStore(tmp_path)
    store.create(CardCore(id="ab000001", title="[S] review"))
    store.append_event("ab000001", "link", "reviewer", link_key="verdict", link_value="PASS")
    rows = store._read_events("ab000001")
    rows.append(
        dict(action="remote_review_supersede", request_id="a" * 64, ts="2099-01-01T00:00:00Z")
    )
    monkeypatch.setattr(store, "_read_events", lambda c: rows)
    monkeypatch.setattr(
        review_retire, "retired_offers", lambda *a: {"a" * 64} if verified else set()
    )
    card = SimpleNamespace(
        id="ab000001", labels=["review"], archived=False, status=SimpleNamespace(value="review")
    )
    assert _awaiting_acceptance(tmp_path, store, card) is not verified
