"""Boundaries for the strict blocker-referent sweep."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import time
from pathlib import Path

import pytest
from skcoord.card import CardEvent, CardEventLog
from skcoord.card_store import CardCore, CardStore

import skcapstone.blocker_referent as blocker_referent
from skcapstone.blocker_referent import (
    LABEL,
    VERDICT_MARKER_KEY,
    apply_candidate,
    apply_candidates,
    exact_card_referents,
    find_returnable,
    find_stale_blocks,
    is_discharging_pass,
    verdict_head,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "blocker_referent_sweep_script",
        ROOT / "scripts" / "fleet" / "blocker-referent-sweep.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_script()


def _card(
    home: Path,
    card_id: str,
    *,
    done: bool = False,
    labels: tuple[str, ...] = (),
    archived: bool = False,
    voided: bool = False,
) -> None:
    store = CardStore(home)
    store.create(
        CardCore(
            id=card_id,
            title=card_id,
            created_by="test",
            initial_labels=list(labels),
        )
    )
    if done:
        store.append_event(card_id, "complete", "test")
    if archived:
        store.append_event(card_id, "archive", "test")
    if voided:
        store.append_event(card_id, "void", "test", reason="test")


def _outcome(home: Path, card_id: str, value: str, ts: str, writer: str = "reviewer") -> None:
    CardEventLog(home).append(
        CardEvent(
            card_id=card_id,
            action="link",
            link_key="verdict",
            link_value=value,
            writer=writer,
            ts=ts,
        )
    )


def _write_label(home, candidate, label, agent):
    CardEventLog(home).append(
        CardEvent(
            card_id=candidate.card_id,
            action="add_label",
            label=label,
            writer=agent,
            link_key=VERDICT_MARKER_KEY,
            link_value=candidate.verdict.identity,
        )
    )


def _slow_label(home, candidate, label, agent):
    time.sleep(0.2)
    _write_label(home, candidate, label, agent)


def _concurrent_apply(home: str, candidate, agent: str, gate, results) -> None:
    gate.wait()
    receipt = apply_candidate(
        Path(home),
        candidate,
        agent=agent,
        writer=_slow_label,
    )
    results.put(receipt.state)


def _seed_returnable(home: Path, target: str = "11111111", referent: str = "aaaaaaaa"):
    _card(home, target)
    _card(home, referent, done=True)
    _outcome(
        home,
        target,
        f"BLOCKED blocked_on=card referent=card:{referent}",
        "2026-01-01T00:00:00+00:00",
    )
    return find_returnable(home).candidates[0]


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            "BLOCKED blocked_on=card referent=card:abcdef12",
            ("abcdef12",),
        ),
        (
            "BLOCKED blocked_on=card referent=card:abcdef12 referent=card:1234abcd",
            ("abcdef12", "1234abcd"),
        ),
    ],
)
def test_exact_contract_accepts_only_distinct_lowercase_cards(value, expected):
    assert exact_card_referents(value) == (expected, None)


@pytest.mark.parametrize(
    "value",
    [
        "BLOCKED blocked_on=card",
        "BLOCKED blocked_on=card referent=abcdef12",
        "BLOCKED blocked_on=card referent=card:abcdef123",
        "BLOCKED blocked_on=card referent=card:abcdef12-extra",
        "BLOCKED blocked_on=card referent=card:ABCDEF12",
        "BLOCKED blocked_on=card referent=card:abcdef1",
        "BLOCKED blocked_on=card referent=card:zzzzzzzz",
        "BLOCKED blocked_on=card referent=card:abcdef12 referent=card:abcdef12",
        "BLOCKED blocked_on=human referent=card:abcdef12",
        "BLOCKED blocked_on=capability referent=card:abcdef12",
        "BLOCKED blocked_on=dependency referent=card:abcdef12",
        "BLOCKED blocked_on=card blocked_on=human referent=card:abcdef12",
        "BLOCKED blocked_on=card referent=card:abcdef12 referent=approval:owner",
        "BLOCKED blocked_on=card referent=card:abcdef12 ac:1",
        "BLOCKED blocked_on=card referent=card:abcdef12 ac=1",
        "BLOCKED blocked_on=card-extra referent=card:abcdef12",
        "blocked blocked_on=card referent=card:abcdef12",
    ],
)
def test_ambiguous_or_malformed_verdicts_fail_closed(value):
    referents, error = exact_card_referents(value)
    assert referents == ()
    assert error


def test_latest_pass_supersedes_blocked(tmp_path):
    _seed_returnable(tmp_path)
    _outcome(tmp_path, "11111111", "PASS", "2026-01-02T00:00:00+00:00")
    report = find_returnable(tmp_path)
    assert report.candidates == []
    assert "11111111" not in report.held


def test_same_timestamp_uses_writer_as_the_canonical_tie_breaker(tmp_path):
    _seed_returnable(tmp_path)
    _outcome(
        tmp_path,
        "11111111",
        "PASS",
        "2026-01-01T00:00:00+00:00",
        writer="zzz-reviewer",
    )
    assert find_returnable(tmp_path).candidates == []


@pytest.mark.parametrize(
    "state,expected",
    [
        ("missing", "missing"),
        ("open", "not-DONE"),
        ("voided", "voided"),
        ("superseded", "superseded"),
        ("archived", "archived"),
    ],
)
def test_referent_must_be_active_done_under_canonical_fold(tmp_path, state, expected):
    target, referent = "11111111", "aaaaaaaa"
    _card(tmp_path, target)
    if state == "open":
        _card(tmp_path, referent)
    elif state == "voided":
        _card(tmp_path, referent, done=True, voided=True)
    elif state == "superseded":
        _card(tmp_path, referent, done=True, labels=("superseded",))
    elif state == "archived":
        _card(tmp_path, referent, done=True, archived=True)
    _outcome(
        tmp_path,
        target,
        f"BLOCKED blocked_on=card referent=card:{referent}",
        "2026-01-01T00:00:00+00:00",
    )
    report = find_returnable(tmp_path)
    assert report.candidates == []
    assert expected in report.held[target]


def test_every_referent_must_be_done(tmp_path):
    _card(tmp_path, "11111111")
    _card(tmp_path, "aaaaaaaa", done=True)
    _card(tmp_path, "bbbbbbbb")
    _outcome(
        tmp_path,
        "11111111",
        "BLOCKED blocked_on=card referent=card:aaaaaaaa referent=card:bbbbbbbb",
        "2026-01-01T00:00:00+00:00",
    )
    report = find_returnable(tmp_path)
    assert report.candidates == []
    assert "bbbbbbbb:not-DONE" in report.held["11111111"]


def test_report_only_makes_no_writes(tmp_path, capsys):
    _seed_returnable(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert SCRIPT.main(["--home", str(tmp_path)]) == 0
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert "report only; no labels written" in capsys.readouterr().out


def test_custom_home_is_used_for_read_and_write(tmp_path):
    candidate = _seed_returnable(tmp_path)
    seen = []

    def writer(home, item, label, agent):
        seen.append(home)
        _write_label(home, item, label, agent)

    receipt = apply_candidate(
        tmp_path,
        candidate,
        agent="home-test",
        writer=writer,
    )
    assert receipt.state == "labelled"
    assert seen == [tmp_path]
    labels = [event for event in CardEventLog(tmp_path).read_all() if event.action == "add_label"]
    assert len(labels) == 1


def test_same_verdict_is_idempotent_and_new_verdict_requalifies(tmp_path):
    candidate = _seed_returnable(tmp_path)
    first = apply_candidate(
        tmp_path,
        candidate,
        agent="test",
        writer=_write_label,
    )
    second = apply_candidate(
        tmp_path,
        candidate,
        agent="test",
        writer=lambda *_args: pytest.fail("same verdict wrote twice"),
    )
    assert first.state == "labelled"
    assert second.state == "skipped"

    _outcome(
        tmp_path,
        "11111111",
        "BLOCKED blocked_on=card referent=card:aaaaaaaa",
        "2026-01-03T00:00:00+00:00",
    )
    newer = find_returnable(tmp_path).candidates
    assert len(newer) == 1
    assert newer[0].verdict.identity != candidate.verdict.identity
    receipt = apply_candidate(
        tmp_path,
        newer[0],
        agent="test",
        writer=_write_label,
    )
    assert receipt.state == "labelled"
    labels = [event for event in CardEventLog(tmp_path).read_all() if event.action == "add_label"]
    assert len(labels) == 2


def test_identical_new_verdict_event_gets_a_distinct_marker(tmp_path):
    candidate = _seed_returnable(tmp_path)
    assert apply_candidate(tmp_path, candidate, agent="test").state == "labelled"
    _outcome(
        tmp_path,
        candidate.card_id,
        candidate.verdict.value,
        candidate.verdict.ts,
        writer=candidate.verdict.writer,
    )
    newer = find_returnable(tmp_path).candidates
    assert len(newer) == 1
    assert newer[0].verdict.identity != candidate.verdict.identity


def test_two_processes_apply_one_label_for_one_verdict(tmp_path):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires fork to share the test runner")
    candidate = _seed_returnable(tmp_path)
    context = multiprocessing.get_context("fork")
    gate = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_apply,
            args=(str(tmp_path), candidate, agent, gate, results),
        )
        for agent in ("first-agent", "second-agent")
    ]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    states = sorted(results.get(timeout=2) for _ in processes)
    assert states == ["labelled", "skipped"]
    labels = [event for event in CardEventLog(tmp_path).read_all() if event.action == "add_label"]
    assert len(labels) == 1


def test_write_then_error_is_verified_as_success_and_not_repeated(tmp_path):
    candidate = _seed_returnable(tmp_path)

    def write_then_error(home, item, label, agent):
        _write_label(home, item, label, agent)
        raise OSError("lost acknowledgement")

    receipt = apply_candidate(
        tmp_path,
        candidate,
        agent="test",
        writer=write_then_error,
    )
    retry = apply_candidate(
        tmp_path,
        candidate,
        agent="test",
        writer=lambda *_args: pytest.fail("durable marker was repeated"),
    )
    assert receipt.state == "labelled"
    assert "lost acknowledgement" in receipt.detail
    assert retry.state == "skipped"
    markers = [
        event
        for event in CardEventLog(tmp_path).read_all()
        if event.action == "add_label" and event.link_key == VERDICT_MARKER_KEY
    ]
    assert len(markers) == 1


def test_newer_verdict_invalidates_stale_candidate_before_write(tmp_path):
    candidate = _seed_returnable(tmp_path)
    _outcome(tmp_path, candidate.card_id, "PASS", "2026-01-02T00:00:00+00:00")
    receipt = apply_candidate(
        tmp_path,
        candidate,
        agent="test",
        writer=lambda *_args: pytest.fail("stale candidate wrote"),
    )
    assert receipt.state == "skipped"
    assert receipt.detail == "verdict changed"


def test_target_completion_after_scan_skips_write(tmp_path):
    candidate = _seed_returnable(tmp_path)
    CardStore(tmp_path).append_event(candidate.card_id, "complete", "other")
    receipt = apply_candidate(
        tmp_path,
        candidate,
        agent="test",
        writer=lambda *_args: pytest.fail("completed target wrote"),
    )
    assert receipt.state == "skipped"
    assert receipt.detail == "target-DONE"


def test_total_failure_is_non_success_with_detail(tmp_path):
    candidate = _seed_returnable(tmp_path)

    def fail(*_args):
        raise OSError("total write failure detail")

    receipts = apply_candidates(
        tmp_path,
        [candidate],
        agent="test",
        writer=fail,
    )
    assert [(item.state, item.returncode) for item in receipts] == [("failed", 1)]
    assert receipts[0].detail == "total write failure detail"


def test_partial_failure_returns_nonzero_and_keeps_per_card_detail(tmp_path, monkeypatch, capsys):
    _seed_returnable(tmp_path, "11111111", "aaaaaaaa")
    _seed_returnable(tmp_path, "22222222", "bbbbbbbb")

    original = blocker_referent._append_label

    def flaky(home, candidate, label, agent):
        if candidate.card_id == "22222222":
            raise OSError("forced per-card failure")
        original(home, candidate, label, agent)

    monkeypatch.setattr(blocker_referent, "_append_label", flaky)

    assert SCRIPT.main(["--go", "--home", str(tmp_path), "--agent", "partial-test"]) == 1
    captured = capsys.readouterr()
    assert "forced per-card failure" in captured.err
    assert "failed 22222222" in captured.err
    labels = [
        event.card_id
        for event in CardEventLog(tmp_path).read_all()
        if event.action == "add_label" and event.label == LABEL
    ]
    assert labels == ["11111111"]


def test_marker_payload_folds_only_as_the_fixed_label(tmp_path):
    candidate = _seed_returnable(tmp_path)
    receipt = apply_candidate(tmp_path, candidate, agent="fold-test")
    assert receipt.state == "labelled"

    card = CardStore(tmp_path).fold(candidate.card_id)
    assert card is not None
    assert LABEL in card.labels
    assert VERDICT_MARKER_KEY not in card.links
    marker = [
        event
        for event in CardEventLog(tmp_path).read_all()
        if event.action == "add_label" and event.label == LABEL
    ]
    assert len(marker) == 1
    assert marker[0].link_key == VERDICT_MARKER_KEY
    assert marker[0].link_value == candidate.verdict.identity


def test_label_rollback_preserves_once_per_verdict_audit(tmp_path):
    candidate = _seed_returnable(tmp_path)
    assert apply_candidate(tmp_path, candidate, agent="test").state == "labelled"
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id=candidate.card_id,
            action="remove_label",
            label=LABEL,
            writer="rollback-test",
        )
    )
    folded = CardStore(tmp_path).fold(candidate.card_id)
    assert folded is not None
    assert LABEL not in folded.labels
    report = find_returnable(tmp_path)
    assert report.candidates == []
    assert report.held[candidate.card_id] == "already-returned-for-verdict"


# --- blocks answered by the card's own repair ---------------------------------
#
# Thirteen cards on the live board read BLOCKED while the repair or re-review
# they themselves named had already passed, most within fifteen minutes. Nothing
# propagates a successor's PASS back to the card it repairs.


def _link(home: Path, card_id: str, key: str, target: str, ts: str) -> None:
    CardEventLog(home).append(
        CardEvent(
            card_id=card_id,
            action="link",
            link_key=key,
            link_value=target,
            writer="reviewer",
            ts=ts,
        )
    )


def _legacy_link(home: Path, card_id: str, key: str, value: str, ts: str) -> None:
    path = home / "coordination" / "card_events" / "legacy.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "card_id": card_id,
        "action": "link",
        "key": key,
        "value": value,
        "writer": "legacy-reviewer",
        "ts": ts,
        "seq": 0,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row) + "\n")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("PASS|independent-rereview=0380f134|historical-BLOCKED-superseded", "PASS"),
        ("BLOCKED blocked_on=card referent=card:abc12345", "BLOCKED"),
        ("BLOCKED: three independent slices agree", "BLOCKED"),
    ],
)
def test_the_verdict_is_read_from_its_leading_token(value, expected):
    """A PASS explaining what it supersedes contains BLOCKED in prose."""
    assert verdict_head(value) == expected


@pytest.mark.parametrize(
    "value",
    ["PASS_FOR_INDEPENDENT_REVIEW", "PASS_READY_FOR_INDEPENDENT_REREVIEW", "PASS-FOR-REVIEW"],
)
def test_work_awaiting_its_own_review_is_not_a_pass(value):
    assert not is_discharging_pass(value)


@pytest.mark.parametrize("value", ["PASS", "PASS. verified clean.", "PASS|all-CI-green"])
def test_a_completed_pass_discharges(value):
    assert is_discharging_pass(value)


def test_a_block_is_flagged_when_its_named_repair_later_passed(tmp_path):
    _card(tmp_path, "aaaaaaaa")
    _card(tmp_path, "bbbbbbbb", done=True)
    _outcome(tmp_path, "aaaaaaaa", "BLOCKED runbook contradicts custody", "2026-08-23T07:12:00")
    _link(tmp_path, "aaaaaaaa", "independent_rereview", "bbbbbbbb", "2026-08-23T07:12:01")
    _outcome(tmp_path, "bbbbbbbb", "PASS verified clean", "2026-08-23T07:17:00")
    stale = find_stale_blocks(tmp_path)
    assert [row.card_id for row in stale] == ["aaaaaaaa"]
    assert stale[0].successor == "bbbbbbbb"


def test_legacy_key_value_links_remain_visible_to_both_sweeps(tmp_path):
    _card(tmp_path, "aaaaaaaa")
    _card(tmp_path, "bbbbbbbb", done=True)
    _legacy_link(
        tmp_path,
        "aaaaaaaa",
        "verdict",
        "BLOCKED blocked_on=card referent=card:bbbbbbbb",
        "2026-08-23T07:12:00",
    )
    assert [row.card_id for row in find_returnable(tmp_path).candidates] == ["aaaaaaaa"]

    _legacy_link(
        tmp_path,
        "aaaaaaaa",
        "rereview_card",
        "bbbbbbbb",
        "2026-08-23T07:12:01",
    )
    _legacy_link(
        tmp_path,
        "bbbbbbbb",
        "verdict",
        "PASS verified clean",
        "2026-08-23T07:17:00",
    )
    assert [row.card_id for row in find_stale_blocks(tmp_path)] == ["aaaaaaaa"]


def test_a_pass_recorded_before_the_block_does_not_clear_it(tmp_path):
    """An earlier pass answered a previous refusal, not this one."""
    _card(tmp_path, "aaaaaaaa")
    _card(tmp_path, "bbbbbbbb", done=True)
    _outcome(tmp_path, "bbbbbbbb", "PASS verified clean", "2026-08-23T06:00:00")
    _outcome(tmp_path, "aaaaaaaa", "BLOCKED a new and different problem", "2026-08-23T07:12:00")
    _link(tmp_path, "aaaaaaaa", "independent_rereview", "bbbbbbbb", "2026-08-23T07:12:01")
    assert find_stale_blocks(tmp_path) == []


def test_a_provisional_pass_leaves_the_block_standing(tmp_path):
    """6dd21df9 reached PASS_FOR_INDEPENDENT_REVIEW; its review then blocked."""
    _card(tmp_path, "aaaaaaaa")
    _card(tmp_path, "bbbbbbbb")
    _outcome(tmp_path, "aaaaaaaa", "BLOCKED trust roots not fail-closed", "2026-08-23T09:42:00")
    _link(tmp_path, "aaaaaaaa", "followup_repair", "bbbbbbbb", "2026-08-23T09:42:01")
    _outcome(tmp_path, "bbbbbbbb", "PASS_FOR_INDEPENDENT_REVIEW", "2026-08-23T09:54:53")
    assert find_stale_blocks(tmp_path) == []


def test_a_successor_that_also_blocked_does_not_clear_it(tmp_path):
    _card(tmp_path, "aaaaaaaa")
    _card(tmp_path, "bbbbbbbb")
    _outcome(tmp_path, "aaaaaaaa", "BLOCKED", "2026-08-23T07:12:00")
    _link(tmp_path, "aaaaaaaa", "rereview_card", "bbbbbbbb", "2026-08-23T07:12:01")
    _outcome(tmp_path, "bbbbbbbb", "BLOCKED same problem persists", "2026-08-23T07:17:00")
    assert find_stale_blocks(tmp_path) == []


def test_a_closed_card_with_a_stale_block_is_still_flagged(tmp_path):
    """2c35d28b folded to DONE and still held four approval gates shut.

    A stale verdict does its damage through whoever reads it, not through the
    card's own column, so closed cards must not be filtered out here. This is
    the deliberate difference from find_returnable.
    """
    _card(tmp_path, "aaaaaaaa", done=True)
    _card(tmp_path, "bbbbbbbb", done=True)
    _outcome(tmp_path, "aaaaaaaa", "BLOCKED", "2026-08-23T07:12:00")
    _link(tmp_path, "aaaaaaaa", "rereview_card", "bbbbbbbb", "2026-08-23T07:12:01")
    _outcome(tmp_path, "bbbbbbbb", "PASS", "2026-08-23T07:17:00")
    assert [row.card_id for row in find_stale_blocks(tmp_path)] == ["aaaaaaaa"]
    assert find_returnable(tmp_path).held.get("aaaaaaaa") == "target-DONE"


def test_a_card_whose_own_latest_verdict_is_now_pass_is_not_stale(tmp_path):
    _card(tmp_path, "aaaaaaaa")
    _card(tmp_path, "bbbbbbbb", done=True)
    _outcome(tmp_path, "aaaaaaaa", "BLOCKED", "2026-08-23T07:12:00")
    _link(tmp_path, "aaaaaaaa", "rereview_card", "bbbbbbbb", "2026-08-23T07:12:01")
    _outcome(tmp_path, "bbbbbbbb", "PASS", "2026-08-23T07:17:00")
    _outcome(tmp_path, "aaaaaaaa", "PASS|supersedes historical-BLOCKED", "2026-08-28T22:39:00")
    assert find_stale_blocks(tmp_path) == []
