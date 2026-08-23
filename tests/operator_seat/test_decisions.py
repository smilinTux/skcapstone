"""Tests for the O5a pending-decision store (park/list/resolve, no actuation)."""

from __future__ import annotations

import pytest

from skcapstone.operator_seat.decisions import list_pending, park, resolve


def _option(action="restart_service", change_class="standard", dry_run=False, rationale="r"):
    return {
        "action": action,
        "change_class": change_class,
        "dry_run": dry_run,
        "rationale": rationale,
    }


def test_park_and_list_pending_round_trip(tmp_path):
    record = park(
        tmp_path,
        [_option()],
        decision_id="d1",
        created_iso="2026-07-29T00:00:00Z",
    )
    assert record == {
        "id": "d1",
        "created": "2026-07-29T00:00:00Z",
        "status": "pending",
        "options": [_option()],
        "chosen": None,
        "resolved_by": None,
        "resolved_at": None,
    }
    pending = list_pending(tmp_path)
    assert pending == [record]


def test_list_pending_sorted_by_created(tmp_path):
    park(tmp_path, [_option()], decision_id="later", created_iso="2026-07-29T02:00:00Z")
    park(tmp_path, [_option()], decision_id="earlier", created_iso="2026-07-29T01:00:00Z")
    pending = list_pending(tmp_path)
    assert [r["id"] for r in pending] == ["earlier", "later"]


def test_list_pending_empty_dir(tmp_path):
    assert list_pending(tmp_path / "does-not-exist") == []


def test_resolve_approve_single_option(tmp_path):
    park(tmp_path, [_option()], decision_id="d1", created_iso="2026-07-29T00:00:00Z")
    record = resolve(
        tmp_path,
        "d1",
        approve=True,
        choice=None,
        by="alice",
        resolved_iso="2026-07-29T01:00:00Z",
    )
    assert record["status"] == "approved"
    assert record["chosen"] == 0
    assert record["resolved_by"] == "alice"
    assert record["resolved_at"] == "2026-07-29T01:00:00Z"
    assert list_pending(tmp_path) == []


def test_resolve_approve_choosing_among_three_options(tmp_path):
    options = [_option("a"), _option("b"), _option("c")]
    park(tmp_path, options, decision_id="d1", created_iso="2026-07-29T00:00:00Z")
    record = resolve(
        tmp_path,
        "d1",
        approve=True,
        choice=2,
        by="alice",
        resolved_iso="2026-07-29T01:00:00Z",
    )
    assert record["status"] == "approved"
    assert record["chosen"] == 2


def test_resolve_reject(tmp_path):
    park(tmp_path, [_option()], decision_id="d1", created_iso="2026-07-29T00:00:00Z")
    record = resolve(
        tmp_path,
        "d1",
        approve=False,
        choice=None,
        by="alice",
        resolved_iso="2026-07-29T01:00:00Z",
    )
    assert record["status"] == "rejected"
    assert record["chosen"] is None


def test_resolve_by_operator_raises(tmp_path):
    park(tmp_path, [_option()], decision_id="d1", created_iso="2026-07-29T00:00:00Z")
    with pytest.raises(ValueError):
        resolve(
            tmp_path,
            "d1",
            approve=True,
            choice=None,
            by="operator",
            resolved_iso="2026-07-29T01:00:00Z",
        )


def test_resolve_unknown_id_raises(tmp_path):
    with pytest.raises(ValueError):
        resolve(
            tmp_path,
            "missing",
            approve=True,
            choice=None,
            by="alice",
            resolved_iso="2026-07-29T01:00:00Z",
        )


def test_resolve_already_resolved_raises(tmp_path):
    park(tmp_path, [_option()], decision_id="d1", created_iso="2026-07-29T00:00:00Z")
    resolve(
        tmp_path,
        "d1",
        approve=True,
        choice=None,
        by="alice",
        resolved_iso="2026-07-29T01:00:00Z",
    )
    with pytest.raises(ValueError):
        resolve(
            tmp_path,
            "d1",
            approve=False,
            choice=None,
            by="alice",
            resolved_iso="2026-07-29T02:00:00Z",
        )


def test_resolve_choice_out_of_range_raises(tmp_path):
    options = [_option("a"), _option("b"), _option("c")]
    park(tmp_path, options, decision_id="d1", created_iso="2026-07-29T00:00:00Z")
    with pytest.raises(ValueError):
        resolve(
            tmp_path,
            "d1",
            approve=True,
            choice=5,
            by="alice",
            resolved_iso="2026-07-29T01:00:00Z",
        )


def test_park_is_create_or_skip_dedup(tmp_path):
    from skcapstone.operator_seat import decisions

    d = str(tmp_path / "dec")
    r1 = decisions.park(
        d,
        [{"action": "restart_service", "object": "web"}],
        decision_id="k1",
        created_iso="2026-07-29T00:00:00Z",
    )
    # re-parking the same id (persistent firing) must not duplicate or overwrite
    r2 = decisions.park(
        d,
        [{"action": "restart_service", "object": "web"}],
        decision_id="k1",
        created_iso="2026-07-29T00:15:00Z",
    )
    assert r2["created"] == r1["created"]  # original preserved, not re-stamped
    assert len(decisions.list_pending(d)) == 1  # one decision, not two
