"""The rotator names why a source review plan was refused instead of skipping it."""

from __future__ import annotations

from pathlib import Path

from skcapstone.fleet import refusal_log
from tests.test_skfleet_provisional_opener import OpenerHarness


def _production_board(tmp_path: Path) -> OpenerHarness:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4")
    board.states["a1b2c3d4"] = "claimed"
    board.ns.update(
        Path=Path,
        PRODUCTION_POLICY={"authority_host": "control"},
        HOST="control",
        _card_process_snapshot=lambda card: {"sessions": [], "units": []},
        _log_once_per_hour=lambda dest, event, cid, message: board.logs.append(message),
    )
    return board


def test_refused_source_review_plan_logs_reason(tmp_path: Path) -> None:
    refusal_log._LAST.clear()
    refusal_log._LOGGED.clear()
    board = _production_board(tmp_path)
    # No native card exists in the temp CardStore, so planning is refused.
    assert board.ns["_eligible_provisional_reviews"](1) == []
    refused = [row for row in board.logs if row.startswith("OPEN_REVIEW_PLAN_REFUSED|")]
    assert len(refused) == 1
    assert "|a1b2c3d4|" in refused[0]
    assert "reason=unspecified" not in refused[0]
    assert refusal_log.last_reason("a1b2c3d4") in refused[0]


def test_wrong_host_plan_refusal_reason_is_reported(tmp_path: Path, monkeypatch) -> None:
    refusal_log._LAST.clear()
    board = _production_board(tmp_path)
    board.ns["HOST"] = "elsewhere"
    monkeypatch.setattr(
        "skcapstone.seraph_review_cardstore.LiveCardStoreGateway.read_card",
        lambda self, card_id, _store=None: type("S", (), {"revision": "r"})(),
    )
    plan = board.ns["_production_review_plan"](
        "a1b2c3d4", "2026-09-01T12:00:00Z", ("p", "/x", "d", "c", "t", "r")
    )
    assert plan is None
    assert refusal_log.last_reason("a1b2c3d4") == "host is not the authority host"
