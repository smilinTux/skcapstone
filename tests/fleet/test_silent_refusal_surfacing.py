"""Fail-closed gates must say why they refused, without changing the decision."""

from __future__ import annotations

import logging
import types
from pathlib import Path

import pytest

from skcapstone import review_admission
from skcapstone.fleet import production_custody, refusal_log, review_dispatch
from skcapstone.fleet.paths import FleetPaths


@pytest.fixture(autouse=True)
def _fresh_dedup():
    refusal_log._LOGGED.clear()
    refusal_log._LAST.clear()
    yield
    refusal_log._LOGGED.clear()
    refusal_log._LAST.clear()


def _messages(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_refuse_returns_false_logs_once_per_site_subject_reason(caplog):
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        assert refusal_log.refuse("gate", "8f4b04d4aa", "because") is False
        assert refusal_log.refuse("gate", "8f4b04d4aa", "because") is False
        assert refusal_log.refuse("gate", "8f4b04d4aa", "other") is False
    msgs = _messages(caplog)
    assert msgs == [
        "GATE_REFUSED|gate|8f4b04d4aa|because",
        "GATE_REFUSED|gate|8f4b04d4aa|other",
    ]
    assert refusal_log.last_reason("8f4b04d4aa") == "other"


def test_note_records_reason_without_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        refusal_log.note("c1", "expected deferral")
    assert not _messages(caplog)
    assert refusal_log.last_reason("c1") == "expected deferral"


class _Card:
    def __init__(self, owner="someone", labels=(), meta=None):
        self.owner = owner
        self.labels = list(labels)
        self.meta = meta or {}


class _Store:
    def __init__(self, card):
        self.card = card

    def fold(self, card_id):
        return self.card


def _candidate(card, **kw):
    return production_custody.reviewable_source_candidate(
        Path("/nonexistent-home"),
        "8f4b04d4",
        "2026-10-12T00:00:00Z",
        ("producer", "/p", "a" * 64, "b" * 40, "c" * 40, "refs/heads/x"),
        policy=kw.get("policy", {"authority_host": "h"}),
        process_check=lambda cid: {"sessions": [], "units": []},
        store=_Store(card),
    )


@pytest.mark.parametrize(
    ("card", "policy", "reason"),
    [
        (_Card("producer"), {}, "no production policy"),
        (None, {"authority_host": "h"}, "card not found"),
        (_Card("other"), {"authority_host": "h"}, "card owner differs from verdict producer"),
        (
            _Card("producer", meta={"claim_conflicts": [1]}),
            {"authority_host": "h"},
            "claim conflicts present",
        ),
        (
            _Card("producer", labels=["hold"]),
            {"authority_host": "h"},
            "card carries hold or do-not-claim",
        ),
    ],
)
def test_source_review_candidate_refusal_reason_is_logged(caplog, card, policy, reason):
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        assert _candidate(card, policy=policy) is False
    assert _messages(caplog) == [f"GATE_REFUSED|source-review-candidate|8f4b04d4|{reason}"]


def test_source_review_candidate_custody_refusal_names_the_gate(caplog, monkeypatch):
    monkeypatch.setattr(production_custody, "retains_source_custody", lambda *a, **k: False)
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        assert _candidate(_Card("producer")) is False
    assert _messages(caplog) == [
        "GATE_REFUSED|source-review-candidate|8f4b04d4|source custody not retained"
    ]


def test_source_review_candidate_exception_is_not_swallowed_silently(caplog, monkeypatch):
    def boom(*a, **k):
        raise OSError("mount gone")

    monkeypatch.setattr(production_custody, "retains_source_custody", boom)
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        assert _candidate(_Card("producer")) is False
    assert _messages(caplog) == [
        "GATE_REFUSED|source-review-candidate|8f4b04d4|OSError: mount gone"
    ]


@pytest.fixture
def offer_env(tmp_path, monkeypatch):
    policy = {
        "remote_review": {"enabled": True, "card_ids": None, "destinations": []},
        "authority_host": "niobe",
    }
    monkeypatch.setattr(review_dispatch.production, "policy", lambda: policy)
    monkeypatch.setattr(review_dispatch.socket, "gethostname", lambda: "niobe")
    paths = FleetPaths(tmp_path / "fleet")
    paths.root.mkdir()
    writer = types.SimpleNamespace(role="scheduler", node="niobe", identity="niobe")
    return paths, tmp_path, writer, policy


def test_offer_deferral_reason_is_reported_for_disabled_remote_review(monkeypatch, offer_env):
    paths, home, writer, policy = offer_env
    policy["remote_review"]["enabled"] = False
    assert review_dispatch.offer_review(paths, home, "8f4b04d4", writer=writer) is None
    assert refusal_log.last_reason("8f4b04d4") == "remote review disabled"


def test_offer_deferral_reason_is_reported_when_actuation_is_paused(monkeypatch, offer_env):
    paths, home, writer, _ = offer_env
    monkeypatch.setattr(review_dispatch.store, "actuation_allowed", lambda p: False)
    assert review_dispatch.offer_review(paths, home, "8f4b04d4", writer=writer) is None
    assert refusal_log.last_reason("8f4b04d4") == "fleet actuation paused"


class _OfferStore:
    def __init__(self, home):
        pass

    def _read_events(self, card_id):
        return []

    def fold(self, card_id):
        return types.SimpleNamespace(labels=[], id=card_id)


def _ready_offer_env(monkeypatch, offer_env):
    paths, home, writer, _ = offer_env
    monkeypatch.setattr(review_dispatch.store, "actuation_allowed", lambda p: True)
    monkeypatch.setattr(review_dispatch, "CardStore", _OfferStore)
    monkeypatch.setattr(review_dispatch.dispatch, "held_card_ids", lambda p: set())
    return paths, home, writer


def test_offer_refusal_for_unready_review_is_logged_not_just_deferred(
    caplog, monkeypatch, offer_env
):
    paths, home, writer = _ready_offer_env(monkeypatch, offer_env)
    monkeypatch.setattr(review_dispatch, "governed_review_assignment_ready", lambda card: False)
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        assert review_dispatch.offer_review(paths, home, "8f4b04d4", writer=writer) is None
    assert _messages(caplog) == [
        "GATE_REFUSED|review-offer|8f4b04d4|review not ready for assignment"
    ]


@pytest.mark.parametrize(
    ("routes", "ready", "capable", "reason"),
    [
        (
            [],
            [types.SimpleNamespace(name="n1")],
            True,
            "no eligible review route (candidates or producer family)",
        ),
        ([{"family": "f"}], [], True, "no ready builder node"),
        (
            [{"family": "f"}],
            [types.SimpleNamespace(name="n1")],
            False,
            "no node with review capability, allowed destination and safe offer dir",
        ),
        (
            [{"family": "f"}],
            [types.SimpleNamespace(name="n1")],
            True,
            "no node passed production readiness",
        ),
    ],
)
def test_offer_with_no_node_or_route_says_which_stage_emptied(
    caplog, monkeypatch, offer_env, routes, ready, capable, reason
):
    paths, home, writer = _ready_offer_env(monkeypatch, offer_env)
    monkeypatch.setattr(review_dispatch, "governed_review_assignment_ready", lambda card: True)
    monkeypatch.setattr(
        review_dispatch, "_source", lambda h, c: {"owner": "o", "family": "f", "head": "h"}
    )
    monkeypatch.setattr(review_dispatch, "producer_family", lambda *a: "f")
    monkeypatch.setattr(review_dispatch, "review_family_allowed", lambda *a, **k: True)
    monkeypatch.setattr(
        review_dispatch.production.production_routes, "candidates", lambda *a: routes
    )
    monkeypatch.setattr(review_dispatch.production, "ready_nodes", lambda *a: ready)
    monkeypatch.setattr(review_dispatch.dispatch, "_ready_builders", lambda p: [])
    monkeypatch.setattr(review_dispatch, "_capable", lambda p, n: capable)
    monkeypatch.setattr(
        review_dispatch.production,
        "node_binding",
        lambda p, n, pol: {"host": "dest"},
    )
    policy = offer_env[3]
    policy["remote_review"]["destinations"] = ["dest"]
    monkeypatch.setattr(review_dispatch, "_offer_directory_safe", lambda p, n, c: True)
    monkeypatch.setattr(review_dispatch.dispatch, "_production_ready", lambda *a: [])
    monkeypatch.setattr("skcapstone.fleet.production_receipts.card_required_size", lambda c: "S")
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        assert review_dispatch.offer_review(paths, home, "8f4b04d4", writer=writer) is None
    assert _messages(caplog) == [f"GATE_REFUSED|review-offer|8f4b04d4|{reason}"]


def test_unsafe_offer_directory_reason_is_logged(caplog, tmp_path):
    paths = FleetPaths(tmp_path / "fleet")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (paths.root / "dispatch").mkdir(parents=True)
    (paths.root / "dispatch/node-a").symlink_to(target)
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        assert review_dispatch._offer_directory_safe(paths, "node-a", "8f4b04d4") is False
    msgs = _messages(caplog)
    assert len(msgs) == 1
    assert msgs[0].startswith("GATE_REFUSED|offer-directory|8f4b04d4|node=node-a")


def test_review_capacity_evidence_failure_is_logged_and_still_fails_closed(
    caplog, tmp_path, monkeypatch
):
    monkeypatch.setenv("SKFLEET_EVIDENCE_HOST", "chiap01")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "fleet-review-routes.chiap01.json").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger=refusal_log.__name__):
        result = review_admission.reviewer_capacity_evaluation(
            tmp_path, {"title": "[S] x"}, ["review"], "producer", "seraph"
        )
    assert result is not None
    msgs = _messages(caplog)
    assert len(msgs) == 1
    assert msgs[0].startswith("GATE_REFUSED|review-capacity-evidence|seraph|")
