"""P4 alert surface for the fleet suggestion engine (card c6a87139).

Covers:
- ensure_card materializes an ``alert-<id>`` shadow card from an alert_store
  record, carrying the alert's own options in meta.origin.
- The alert's options are surfaced verbatim as suggestions (the actual
  feature Chef asked for), draft-clamped, and never LLM-regenerated.
- gate() has a draft-only row for origin == "alert".
"""

from __future__ import annotations

from skcapstone import agent_run as ar
from skcapstone import alert_store
from skcapstone.card_store import CardStore

# ── shadow card materialization ─────────────────────────────────────


def test_ensure_card_materializes_alert_shadow(tmp_path):
    alert_store.raise_alert(
        tmp_path,
        "gmktec-rma-1",
        "GMKtec warranty RMA follow-up",
        description="vendor email condensed",
        options=[
            {"text": "send the escalation email", "mode": "dry-run"},
            {"text": "snooze a week", "mode": "propose"},
            {"text": "mark resolved", "mode": "propose"},
        ],
        labels=["ops"],
    )
    assert ar.ensure_card(tmp_path, "alert-gmktec-rma-1") is True
    card = CardStore(tmp_path).fold("alert-gmktec-rma-1")
    assert card is not None
    assert card.title == "GMKtec warranty RMA follow-up"
    assert "alert" in card.labels
    assert "ops" in card.labels
    assert card.meta["origin"]["surface"] == "alert"
    assert card.meta["origin"]["id"] == "gmktec-rma-1"
    assert len(card.meta["origin"]["options"]) == 3


def test_ensure_card_unknown_alert_returns_false(tmp_path):
    assert ar.ensure_card(tmp_path, "alert-nope") is False
    assert CardStore(tmp_path).fold("alert-nope") is None


def test_ensure_card_alert_idempotent(tmp_path):
    alert_store.raise_alert(tmp_path, "a1", "title")
    assert ar.ensure_card(tmp_path, "alert-a1") is True
    assert ar.ensure_card(tmp_path, "alert-a1") is True
    assert CardStore(tmp_path).fold("alert-a1") is not None


def test_ensure_card_alert_falls_back_title_when_blank(tmp_path):
    alert_store.raise_alert(tmp_path, "a1", "")
    ar.ensure_card(tmp_path, "alert-a1")
    card = CardStore(tmp_path).fold("alert-a1")
    assert card.title == "Alert a1"


# ── suggestions: the alert's own options, surfaced verbatim ─────────


def test_alert_suggestions_use_the_alerts_own_options(tmp_path):
    alert_store.raise_alert(
        tmp_path,
        "a1",
        "t",
        options=[
            {"text": "send the escalation email", "mode": "dry-run"},
            {"text": "mark resolved", "mode": "propose"},
        ],
    )
    out = ar.suggest_next_steps(tmp_path, "alert-a1", use_llm=False)
    assert out["source"] == "heuristic"
    texts = {s["text"] for s in out["suggestions"]}
    assert "send the escalation email" in texts
    assert "mark resolved" in texts


def test_alert_suggestions_accept_bare_string_options(tmp_path):
    alert_store.raise_alert(tmp_path, "a1", "t", options=["snooze a week"])
    out = ar.suggest_next_steps(tmp_path, "alert-a1", use_llm=False)
    assert out["suggestions"] == [{"text": "snooze a week", "mode": "propose"}]


def test_alert_suggestions_clamp_send_verbs_to_dry_run(tmp_path):
    alert_store.raise_alert(
        tmp_path,
        "a1",
        "t",
        options=[{"text": "Send the escalation email now.", "mode": "execute"}],
    )
    out = ar.suggest_next_steps(tmp_path, "alert-a1", use_llm=False)
    assert out["suggestions"][0]["mode"] == "dry-run"


def test_alert_suggestions_fall_back_to_generic_heuristic_when_no_options(tmp_path):
    alert_store.raise_alert(tmp_path, "a1", "t")
    out = ar.suggest_next_steps(tmp_path, "alert-a1", use_llm=False)
    assert out["source"] == "heuristic"
    assert out["suggestions"] == ar._HEURISTIC_ALERT


def test_alert_suggestions_never_call_the_llm_even_when_use_llm_true(tmp_path, monkeypatch):
    """Design doc section 4.3: untrusted text must never write the option
    list. An alert's options are fixed at raise_alert() time by non-model
    code; suggest_next_steps must not let an LLM re-derive them, even with
    use_llm=True (the default)."""
    alert_store.raise_alert(tmp_path, "a1", "t", options=["snooze"])

    def _blow_up(*args, **kwargs):
        raise AssertionError("the LLM must never be consulted for an alert card")

    monkeypatch.setattr("skcapstone.skgateway_client.chat", _blow_up, raising=False)

    out = ar.suggest_next_steps(tmp_path, "alert-a1", use_llm=True)
    assert out["source"] == "heuristic"
    assert out["suggestions"] == [{"text": "snooze", "mode": "propose"}]


# ── gate() draft-only row ────────────────────────────────────────────


def test_gate_alert_execute_is_draft_only():
    d = ar.gate("task", "execute", origin="alert")
    assert d["allow_execute"] is True
    assert "draft" in d["reason"].lower() or "send" in d["reason"].lower()


def test_gate_alert_execute_never_says_send_is_allowed():
    d = ar.gate("task", "execute", origin="alert")
    assert "unable to send" in d["reason"].lower()
