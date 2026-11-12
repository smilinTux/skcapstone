"""release-claim refuses to release a claim whose holder already recorded PASS*."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore

from skcapstone.cli.coord import register_coord_commands

CARD = "a1b2c3d4"
OWNER = "pi-glm-chiap08-a1b2c3d4"
ACTOR = "niobe"


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _held(home, verdict=None, *, later=None):
    store = CardStore(home)
    store.create(CardCore(id=CARD, kind="task", title="probe", created_by="test"))
    store.append_event(CARD, "claim", OWNER, owner=OWNER)
    revision = store.fold(CARD).meta["_claim_revision"]
    if verdict:
        store.append_event(CARD, "verdict", OWNER, verdict=verdict)
    if later:
        store.append_event(CARD, "verdict", OWNER, verdict=later)
    return revision


def _release(home, revision, *extra):
    return CliRunner().invoke(
        _main(),
        [
            "coord", "release-claim", CARD, "--owner", OWNER,
            "--expected-claim-revision", revision, "--agent", ACTOR,
            "--abandon-reason", "error", "--home", str(home), *extra,
        ],
    )  # fmt: skip


def _owner(home):
    return CardStore(home).fold(CARD).owner


@pytest.fixture(autouse=True)
def _no_worker(monkeypatch):
    monkeypatch.delenv("SKFLEET_CARD_ID", raising=False)


@pytest.mark.parametrize("verdict", ["PASS_FOR_REVIEW", "PASS", "PASS_FOR_REREVIEW"])
def test_pass_holder_refused_with_reason_and_verdict(tmp_path, verdict):
    revision = _held(tmp_path, verdict)
    result = _release(tmp_path, revision)
    assert result.exit_code != 0
    assert verdict in result.output
    assert "Finished work is not stale" in result.output
    assert _owner(tmp_path) == OWNER


def test_operator_override_with_reason_releases_and_audits(tmp_path):
    revision = _held(tmp_path, "PASS_FOR_REVIEW")
    result = _release(
        tmp_path, revision, "--release-finished-work", "--reason", "operator rescue 123"
    )
    assert result.exit_code == 0, result.output
    assert _owner(tmp_path) is None
    event = [e for e in CardStore(tmp_path)._read_events(CARD) if e["action"] == "release_claim"][
        -1
    ]
    assert event["finished_work_override"] is True
    assert event["finished_work_verdict"] == "PASS_FOR_REVIEW"
    assert event["finished_work_override_reason"] == "operator rescue 123"


def test_override_without_reason_refused(tmp_path):
    revision = _held(tmp_path, "PASS_FOR_REVIEW")
    result = _release(tmp_path, revision, "--release-finished-work")
    assert result.exit_code != 0
    assert "--reason" in result.output
    assert _owner(tmp_path) == OWNER


def test_worker_identity_cannot_override(tmp_path, monkeypatch):
    revision = _held(tmp_path, "PASS_FOR_REVIEW")
    monkeypatch.setenv("SKFLEET_CARD_ID", CARD)
    result = _release(tmp_path, revision, "--release-finished-work", "--reason", "x")
    assert result.exit_code != 0
    assert "production worker" in result.output
    assert _owner(tmp_path) == OWNER


def test_non_pass_stale_claim_still_releasable(tmp_path):
    revision = _held(tmp_path)
    assert _release(tmp_path, revision).exit_code == 0
    assert _owner(tmp_path) is None


def test_superseded_pass_is_releasable(tmp_path):
    revision = _held(tmp_path, "PASS_FOR_REVIEW", later="BLOCKED: cannot finish")
    assert _release(tmp_path, revision).exit_code == 0


def test_pass_from_previous_claim_does_not_protect_new_claim(tmp_path):
    first = _held(tmp_path, "PASS_FOR_REVIEW")
    assert _release(tmp_path, first, "--release-finished-work", "--reason", "r").exit_code == 0
    store = CardStore(tmp_path)
    store.append_event(CARD, "claim", OWNER, owner=OWNER)
    second = store.fold(CARD).meta["_claim_revision"]
    assert second != first
    assert _release(tmp_path, second).exit_code == 0


def test_dead_worker_board_reaper_path_unaffected(tmp_path):
    """In-process reapers call Board.release_claim directly and are not gated."""
    from skcoord.coordination import Board

    revision = _held(tmp_path, "PASS_FOR_REVIEW")
    board = Board(tmp_path)
    try:
        board.release_claim(OWNER, CARD, actor=ACTOR, expected_claim_revision=revision)
    except ValueError as exc:  # agent projection absent in this fixture
        assert "not found" in str(exc)
    else:
        assert _owner(tmp_path) is None
