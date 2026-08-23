"""P3 GTD adapter for the fleet suggestion engine.

Covers:
- P3.1 ensure_card materializes a ``gtd-<id>`` shadow card from a GTD item.
- P3.2 GTD-aware suggestions + the outbound draft-by-default clamp (a GTD
  execute never auto-sends; it drafts for review).
"""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone import agent_run as ar
from skcapstone.card_store import CardStore


def _seed_gtd(home, item, list_file="next-actions.json"):
    gtd = Path(home) / "coordination" / "gtd"
    gtd.mkdir(parents=True, exist_ok=True)
    (gtd / list_file).write_text(json.dumps([item]), encoding="utf-8")


# ── P3.1 shadow card materialization ────────────────────────────────


def test_ensure_card_materializes_gtd_shadow(tmp_path):
    _seed_gtd(
        tmp_path,
        {
            "id": "abc123",
            "text": "Email Casey the invoice",
            "context": "@email",
            "privacy": "private",
            "status": "next",
            "source": "manual",
        },
    )
    assert ar.ensure_card(tmp_path, "gtd-abc123") is True
    card = CardStore(tmp_path).fold("gtd-abc123")
    assert card is not None
    assert card.title == "Email Casey the invoice"
    assert "gtd" in card.labels
    assert "@email" in card.labels
    assert card.meta["origin"]["surface"] == "gtd"
    assert card.meta["origin"]["id"] == "abc123"
    assert card.meta["origin"]["list"] == "next-actions"
    assert card.meta["origin"]["privacy"] == "private"


def test_ensure_card_unknown_gtd_returns_false(tmp_path):
    _seed_gtd(tmp_path, {"id": "abc123", "text": "x", "status": "next"})
    assert ar.ensure_card(tmp_path, "gtd-nope") is False
    assert CardStore(tmp_path).fold("gtd-nope") is None


def test_ensure_card_finds_gtd_in_waiting(tmp_path):
    _seed_gtd(
        tmp_path,
        {"id": "w1", "text": "Waiting on refund", "status": "waiting"},
        list_file="waiting-for.json",
    )
    assert ar.ensure_card(tmp_path, "gtd-w1") is True
    assert CardStore(tmp_path).fold("gtd-w1").meta["origin"]["list"] == "waiting-for"


def test_ensure_card_idempotent(tmp_path):
    _seed_gtd(tmp_path, {"id": "abc123", "text": "Do a thing", "status": "next"})
    assert ar.ensure_card(tmp_path, "gtd-abc123") is True
    # second call sees the card already exists, still True, no duplicate
    assert ar.ensure_card(tmp_path, "gtd-abc123") is True
    assert CardStore(tmp_path).fold("gtd-abc123") is not None


# ── P3.2 draft-by-default clamp ─────────────────────────────────────


def test_gtd_heuristics_are_draft_safe(tmp_path):
    _seed_gtd(tmp_path, {"id": "abc123", "text": "Email Casey", "status": "next"})
    out = ar.suggest_next_steps(tmp_path, "gtd-abc123", use_llm=False)
    assert out["source"] == "heuristic"
    assert out["suggestions"]
    # no heuristic instructs an unqualified auto-send
    for s in out["suggestions"]:
        text = s["text"].lower()
        if "send" in text:
            assert "do not send" in text or "never" in text


def test_gate_gtd_execute_is_draft_only():
    d = ar.gate("task", "execute", origin="gtd")
    assert d["allow_execute"] is True
    assert "draft" in d["reason"].lower()
    # non-gtd task execute keeps its normal reason
    assert ar.gate("task", "execute")["allow_execute"] is True


def test_gtd_llm_send_execute_downgraded():
    sug = [
        {"text": "Send the email to Casey now.", "mode": "execute"},
        {"text": "Publish the post immediately.", "mode": "execute"},
        {"text": "Summarize the thread.", "mode": "propose"},
        {"text": "Draft a reply for review.", "mode": "dry-run"},
    ]
    clamped = ar._clamp_gtd_suggestions(sug)
    assert clamped[0]["mode"] == "dry-run"  # send -> downgraded
    assert clamped[1]["mode"] == "dry-run"  # publish -> downgraded
    assert clamped[2]["mode"] == "propose"  # untouched
    assert clamped[3]["mode"] == "dry-run"  # untouched
