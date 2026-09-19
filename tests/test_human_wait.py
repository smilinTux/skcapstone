"""Waiting-on-human queue and claim gate, over a real CardStore fold.

Every card here is created and mutated through ``CardStore``/``CardEventLog``,
never a hand-rolled JSONL replay. A synthetic replay that diverges from the real
fold is how a bug shipped here on 75 of 143 cards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skcoord.card import CardEvent, CardEventLog
from skcoord.card_store import CardCore, CardStore

from skcapstone.human_wait import (
    GTD_SOURCE,
    assert_human_claim,
    gtd_text,
    hold_from_events,
    needed_system,
    sync_gtd,
    waiting_on_human,
)

# The worked example, verbatim from the live board on 2026-08-28.
TAILSCALE_VERDICT = (
    "BLOCKED blocked_on=human referent=approval:perform-tailscale-admin-delete-chiap06"
)
CAPABILITY_VERDICT = "BLOCKED blocked_on=capability referent=free"


def _card(
    store: CardStore,
    card_id: str,
    title: str = "card",
    *,
    labels: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
) -> None:
    """Create one real card. ``CardStore.create`` validates the id as a lock."""
    store.create(
        CardCore(
            id=card_id,
            kind="task",
            title=title,
            description=title,
            created_by="lumina",
            acceptance_criteria=["do the thing"],
            initial_labels=list(labels),
            dependencies=list(dependencies),
        )
    )


def _link(home: Path, card_id: str, key: str, value: str, writer: str) -> None:
    """Record a link the way ``coord link`` does, into the overlay the fold reads."""
    CardEventLog(home).append(
        CardEvent(card_id=card_id, action="link", link_key=key, link_value=value, writer=writer)
    )


def test_human_block_holds_and_survives_an_agent_rewriting_the_verdict(tmp_path: Path) -> None:
    """83e498b6 exactly: the relaunched worker must not clear its own hold."""
    store = CardStore(tmp_path)
    _card(store, "83e498b6", "Remove retired chiap06 Windows device record")
    _link(tmp_path, "83e498b6", "verdict", TAILSCALE_VERDICT, "pi-codex-chiap06")

    with pytest.raises(ValueError, match="human claim denied"):
        assert_human_claim(tmp_path, "83e498b6", "pi-codex-chiap06-83e498b6")

    # The refill wave relaunches and the worker downgrades the verdict to a
    # retryable capability refusal. That is what actually happened, 47 times.
    _link(tmp_path, "83e498b6", "verdict", CAPABILITY_VERDICT, "pi-codex-chiap06")

    with pytest.raises(ValueError, match=r"blocked_on_human=approval:perform-tailscale"):
        assert_human_claim(tmp_path, "83e498b6", "pi-codex-chiap06-83e498b6")

    rows = waiting_on_human(tmp_path)
    assert [row.card_id for row in rows] == ["83e498b6"]
    assert rows[0].needs == "approval:perform-tailscale-admin-delete-chiap06"
    assert rows[0].system == "tailscale"
    assert rows[0].source == "verdict"


def test_a_human_authored_approval_releases_the_card(tmp_path: Path) -> None:
    """Chef acting in the console is the only thing that returns the card."""
    store = CardStore(tmp_path)
    _card(store, "00000001", "needs the console")
    _link(tmp_path, "00000001", "verdict", TAILSCALE_VERDICT, "pi-codex-a")
    assert waiting_on_human(tmp_path)

    _link(tmp_path, "00000001", "human_approval", "APPROVED, device deleted", "chef")

    assert waiting_on_human(tmp_path) == ()
    assert_human_claim(tmp_path, "00000001", "pi-codex-a")


def test_an_agent_cannot_forge_the_approval(tmp_path: Path) -> None:
    """The discharge is authorized by WRITER, not by wording."""
    store = CardStore(tmp_path)
    _card(store, "00000002", "needs the console")
    _link(tmp_path, "00000002", "verdict", TAILSCALE_VERDICT, "pi-codex-b")
    _link(tmp_path, "00000002", "human_approval", "APPROVED by me", "pi-codex-b")

    with pytest.raises(ValueError, match="human claim denied"):
        assert_human_claim(tmp_path, "00000002", "pi-codex-b")
    assert len(waiting_on_human(tmp_path)) == 1


def test_the_operator_is_never_blocked_by_a_hold_held_for_the_operator(tmp_path: Path) -> None:
    """A gate for Chef must not stop Chef."""
    store = CardStore(tmp_path)
    _card(store, "00000003", "needs the console")
    _link(tmp_path, "00000003", "verdict", TAILSCALE_VERDICT, "pi-codex-c")

    assert_human_claim(tmp_path, "00000003", "chef")
    assert_human_claim(tmp_path, "00000003", "human-decision-recorder-chiap08")


def test_label_gate_and_title_gate_refuse_at_the_claim_path(tmp_path: Path) -> None:
    """The pool already refused to SELECT these; now the claim refuses too."""
    store = CardStore(tmp_path)
    _card(store, "00000004", "Approve the vendor contract", labels=("human-gate",))
    _card(store, "00000005", "[HUMAN] Decide the pricing tier")
    _card(store, "00000006", "ordinary work")

    for held in ("00000004", "00000005"):
        with pytest.raises(ValueError, match="human_gate"):
            assert_human_claim(tmp_path, held, "pi-codex-d")
    assert_human_claim(tmp_path, "00000006", "pi-codex-d")

    assert {row.card_id for row in waiting_on_human(tmp_path)} == {"00000004", "00000005"}


def test_removing_the_label_returns_the_card(tmp_path: Path) -> None:
    """A released gate is released: the label is the removable spelling."""
    store = CardStore(tmp_path)
    _card(store, "00000007", "Approve the vendor contract", labels=("human-gate",))
    CardEventLog(tmp_path).append(
        CardEvent(card_id="00000007", action="remove_label", label="human-gate", writer="chef")
    )
    assert_human_claim(tmp_path, "00000007", "pi-codex-e")
    assert waiting_on_human(tmp_path) == ()


def test_other_blocker_categories_are_not_this_queue(tmp_path: Path) -> None:
    """Only `human` is waiting on a person; the rest have their own machinery."""
    store = CardStore(tmp_path)
    _card(store, "00000008", "blocked on a dependency")
    _card(store, "00000009", "blocked on a capability")
    _link(
        tmp_path,
        "00000008",
        "verdict",
        "BLOCKED blocked_on=dependency referent=card:0000000a",
        "pi-codex-f",
    )
    _link(tmp_path, "00000009", "verdict", CAPABILITY_VERDICT, "pi-codex-f")

    assert waiting_on_human(tmp_path) == ()
    assert_human_claim(tmp_path, "00000008", "pi-codex-f")
    assert_human_claim(tmp_path, "00000009", "pi-codex-f")


def test_the_typed_blocked_on_link_is_read_by_the_same_parser(tmp_path: Path) -> None:
    """A machine-readable blocked_on holds exactly as a verdict does."""
    store = CardStore(tmp_path)
    _card(store, "0000000b", "needs a decision")
    _link(tmp_path, "0000000b", "blocked_on", "human referent=approval:pricing-tier", "pi-codex-g")

    rows = waiting_on_human(tmp_path)
    assert [(row.card_id, row.needs, row.source) for row in rows] == [
        ("0000000b", "approval:pricing-tier", "blocked_on")
    ]
    with pytest.raises(ValueError, match="human claim denied"):
        assert_human_claim(tmp_path, "0000000b", "pi-codex-g")


def test_queue_reports_what_the_card_unblocks(tmp_path: Path) -> None:
    """The cost of a hold is the work behind it, so the queue says what that is."""
    store = CardStore(tmp_path)
    _card(store, "0000000c", "needs the console")
    _card(store, "0000000d", "downstream one", dependencies=("0000000c",))
    _card(store, "0000000e", "downstream two", dependencies=("0000000c",))
    _link(tmp_path, "0000000c", "verdict", TAILSCALE_VERDICT, "pi-codex-h")

    rows = waiting_on_human(tmp_path)
    assert rows[0].unblocks == ("0000000d", "0000000e")
    assert rows[0].waited_hours >= 0.0


def test_a_completed_card_is_not_waiting_on_anybody(tmp_path: Path) -> None:
    """A hold on a finished card is history, not a queue entry."""
    store = CardStore(tmp_path)
    _card(store, "0000000f", "needs the console")
    _link(tmp_path, "0000000f", "verdict", TAILSCALE_VERDICT, "pi-codex-i")
    store.append_event("0000000f", "complete", "chef")

    assert waiting_on_human(tmp_path) == ()
    assert_human_claim(tmp_path, "0000000f", "pi-codex-i")


def test_hold_from_events_ignores_non_link_noise() -> None:
    """The fold is over link events; moves and claims say nothing about a hold."""
    assert hold_from_events("00000010", []) is None
    assert (
        hold_from_events(
            "00000010",
            [
                {"action": "move", "column": "doing", "ts": "1", "writer": "a"},
                {"action": "claim", "owner": "a", "ts": "2", "writer": "a"},
            ],
        )
        is None
    )


def test_needed_system_is_a_hint_and_never_swallows_the_referent() -> None:
    """A miss costs a column, never an instruction."""
    assert needed_system("approval:perform-tailscale-admin-delete-chiap06") == "tailscale"
    assert needed_system("approval:credentials-rotation") == "credentials"
    assert needed_system("") == ""


def test_sync_gtd_is_idempotent_on_source_and_card_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running reconciles the row it already wrote; it does not duplicate."""
    pytest.importorskip("skos.gtd_ingest")
    # A tmp_path GTD store is empty, which is exactly what the skos cold-start
    # guard exists to refuse. This IS a genuine fresh init, which is the escape
    # hatch the guard documents; the guard itself stays armed in production.
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")
    store = CardStore(tmp_path)
    _card(store, "00000011", "Remove retired chiap06 Windows device record")
    _link(tmp_path, "00000011", "verdict", TAILSCALE_VERDICT, "pi-codex-j")

    rows = waiting_on_human(tmp_path)
    first = sync_gtd(tmp_path, rows)
    assert [action for _cid, _iid, action in first] == ["created"]

    second = sync_gtd(tmp_path, rows)
    assert [action for _cid, _iid, action in second] == ["unchanged"]
    assert first[0][1] == second[0][1]

    waiting = json.loads((tmp_path / "coordination" / "gtd" / "waiting-for.json").read_text())
    assert len(waiting) == 1
    assert waiting[0]["source"] == GTD_SOURCE
    assert waiting[0]["source_ref"] == "00000011"
    assert "approval:perform-tailscale-admin-delete-chiap06" in waiting[0]["text"]
    assert gtd_text(rows[0]).startswith("[00000011]")
