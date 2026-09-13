"""Exact-generation tests for claim evidence in agent projections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skcoord.card_store import CardStore

from skcapstone.agent_projection import project_claim_revision
from skcapstone.coordination import Board, Task


def claimed_board(home: Path, revision: str = "generation-1") -> Board:
    """Create one claimed card and replace its generated revision deterministically."""
    board = Board(home)
    board.create_task(Task(id="deadbeef", title="projection test"))
    board.claim_task("worker-deadbeef", "deadbeef")
    store = CardStore(home)
    card = store.fold("deadbeef")
    assert card is not None
    store.append_event(
        "deadbeef", "claim", "worker-deadbeef", owner="worker-deadbeef", claim_revision=revision
    )
    return board


def projection(board: Board) -> dict:
    """Read the synthetic agent projection."""
    return json.loads(board.agent_projection_path("worker-deadbeef").read_text(encoding="utf-8"))


def test_exact_revision_is_projected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exact current owner, card, and generation is written."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = claimed_board(tmp_path)

    assert project_claim_revision(tmp_path, "worker-deadbeef", "deadbeef", "generation-1")
    assert projection(board)["_claim_revision"] == "generation-1"


@pytest.mark.parametrize("revision", [None, ""])
def test_missing_revision_is_not_projected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str | None
) -> None:
    """Missing generation evidence fails closed."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = claimed_board(tmp_path)

    assert not project_claim_revision(tmp_path, "worker-deadbeef", "deadbeef", revision)
    assert "_claim_revision" not in projection(board)


def test_mismatched_revision_is_not_projected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generation that never matched the CardStore is rejected."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = claimed_board(tmp_path)

    assert not project_claim_revision(tmp_path, "worker-deadbeef", "deadbeef", "generation-x")
    assert "_claim_revision" not in projection(board)


def test_superseded_revision_cannot_replace_current_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A once-valid generation cannot be written after a newer claim wins."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    board = claimed_board(tmp_path, revision="generation-2")

    assert not project_claim_revision(tmp_path, "worker-deadbeef", "deadbeef", "generation-1")
    assert "_claim_revision" not in projection(board)
