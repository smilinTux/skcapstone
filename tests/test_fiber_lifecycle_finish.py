"""Exact review and trusted test evidence close only the intended native pair."""

import functools
import hashlib
import json

import pytest

from tests.test_fiber_lifecycle_connection import Refused, terminal_review
from tests.test_fiber_lifecycle_connection import rig as rig
from tests.test_provisional_verdict_producer import _run

# isort: split
import fiber_finish as finish
from fiber_refill import digest


@pytest.fixture
def completion(rig):
    """Use the actual native CLI with isolated synthetic reports and test logs."""
    r = rig
    returned = terminal_review(r)
    r["run"](collect_candidate=returned["collect"])
    directory = r["state"] / "lifecycle" / r["token"]
    binding = {
        "source_card": r["card"],
        "source_head": r["packet"]["commit"],
        "source_fingerprint": r["job"]["card_fingerprint"],
        "repository": r["job"]["repository"],
    }
    plan = {
        "schema": "skfleet.test-plan/v1",
        **binding,
        "qualified_at": 900,
        "expires_at": 1100,
        "evidence_sha256": "8" * 64,
        "checks": [
            {
                "id": "unit",
                "argv": ["make", "unit-test"],
                "minimum_passed": 12,
                "allowed_skips": [],
            }
        ],
    }
    log = directory / "unit.log"
    log.write_text("Synthetic test-runner fixture, 12 passed, not live acceptance.\n")
    log.chmod(0o600)
    receipt = {
        "schema": "skfleet.test-receipt/v1",
        **binding,
        "plan_sha256": digest(plan),
        "started_at": 910,
        "finished_at": 950,
        "checks": [
            {
                "id": "unit",
                "argv": ["make", "unit-test"],
                "passed": 12,
                "failed": 0,
                "errors": 0,
                "exit_code": 0,
                "skipped": [],
                "host": "chiap04",
                "unit": "skfleet-test-" + r["card"] + "-" + "7" * 32 + ".service",
                "invocation": "8" * 32,
                "log_file": log.name,
                "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            }
        ],
    }
    for name, value in [("test-plan.json", plan), ("test-receipt.json", receipt)]:
        path = directory / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    calls = []

    def command(home, args):
        calls.append(args)
        result = _run(home, *args)
        if result.exit_code:
            raise Refused(result.output)
        return json.loads(result.output) if "--json" in args else None

    def run(execute=command, **overrides):
        return r["run"](
            collect_candidate=returned["collect"],
            finish=functools.partial(finish.finish_pair, command=execute),
            observe=overrides.pop("observe", lambda run: json.loads(run["receipt"])),
            **overrides,
        )

    return locals()


def test_exact_pair_finishes_once_with_applicability_last(completion):
    c, r = completion, completion["r"]
    result = c["run"]()
    assert result["reviews"][0] == {
        "card": r["card"],
        "review": c["returned"]["review"],
        "state": "source-complete",
        "accepted": True,
        "governed_pr_ci": False,
    }
    for card in (r["card"], c["returned"]["review"]):
        row = r["store"].fold(card)
        assert row.status.value == "done" and row.owner is None
        assert len([e for e in r["store"]._read_events(card) if e["action"] == "complete"]) == 1
        assert "ci_check_docs" not in row.links and "hosted_checks" not in row.links
    links = [e for e in r["store"]._read_events(c["returned"]["review"]) if e["action"] == "link"]
    assert links[-1]["link_key"] == "applicability_receipt"
    before = len(c["calls"])
    assert c["run"]() == result
    assert len(c["calls"]) == before


@pytest.mark.parametrize("lost_step", range(17))
def test_every_lost_native_reply_recovers_without_duplicate_events(completion, lost_step):
    c, r = completion, completion["r"]

    def interrupted(home, args):
        result = c["command"](home, args)
        if len(c["calls"]) == lost_step + 1:
            raise Refused("synthetic lost reply")
        return result

    with pytest.raises(Refused, match="lost reply"):
        c["run"](interrupted)
    assert c["run"]()["reviews"][0]["accepted"] is True
    for card in (r["card"], c["returned"]["review"]):
        events = r["store"]._read_events(card)
        assert len([e for e in events if e["action"] == "complete"]) == 1
        ids = [e.get("transition_id") for e in events if e["action"] == "link"]
        assert len(ids) == len(set(ids))
    links = r["store"]._read_events(c["returned"]["review"])
    assert len([e for e in links if e.get("link_key") == "applicability_receipt"]) == 1


@pytest.mark.parametrize("change", ["tests", "review", "source", "claim", "hold"])
def test_partial_completion_never_adopts_changed_proof_or_authority(completion, change):
    c, r = completion, completion["r"]

    def interrupted(home, args):
        result = c["command"](home, args)
        if len(c["calls"]) == 3:
            raise Refused("synthetic lost reply")
        return result

    with pytest.raises(Refused, match="lost reply"):
        c["run"](interrupted)
    if change == "tests":
        c["log"].write_text("replaced trusted log")
    elif change == "hold":
        (r["config"] / "HOLD").write_text("operator hold")
    elif change == "claim":
        r["store"].append_event(
            r["card"], "claim", r["owner"], owner=r["owner"], claim_revision="f" * 32
        )
    else:
        card = r["card"] if change == "source" else c["returned"]["review"]
        r["store"].append_event(card, "link", "operator", link_key="verdict", link_value="FAIL")
    before = {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    if change == "hold":
        assert c["run"]()["state"] == "admission-held"
    else:
        with pytest.raises(Refused):
            c["run"]()
    assert before == {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}


def test_finished_pair_is_not_requeued_when_scope_is_still_current(completion):
    c, r = completion, completion["r"]
    c["run"]()
    request = r["qualified"] / (c["returned"]["review"] + ".json")
    request.unlink()
    c["run"]()
    assert not request.exists()


def test_new_review_outcome_after_completion_is_not_accepted(completion):
    c, r = completion, completion["r"]
    c["run"]()
    r["store"].append_event(
        c["returned"]["review"], "link", "operator", link_key="verdict", link_value="FAIL"
    )
    before = len(c["calls"])
    with pytest.raises(Refused, match="completed-pair-changed"):
        c["run"]()
    assert len(c["calls"]) == before


@pytest.mark.parametrize("change", ["running", "invocation", "failed", "unknown"])
def test_terminal_ledger_is_not_live_completion_proof(completion, change):
    c, r = completion, completion["r"]

    def observe(run):
        value = json.loads(run["receipt"])
        if change == "running":
            value.update(state="running", main_pid=123)
        elif change == "invocation":
            value["invocation"] = "0" * 32
        elif change == "failed":
            value["exit_code"] = 1
        else:
            value["state"] = "unknown"
        return value

    before = {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    with pytest.raises(Refused, match="unit-changed"):
        c["run"](observe=observe)
    assert before == {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}


def test_completed_receipt_is_historical_not_new_execution_authority(completion):
    c, r = completion, completion["r"]
    result = c["run"]()
    producer = json.loads((c["directory"] / "candidate-intent.json").read_text())
    review = json.loads((c["directory"] / "review-intent.json").read_text())

    def no_writes(*args, **kwargs):
        raise AssertionError("Historical completed receipt must not execute or authorize writes")

    from fiber_lifecycle import _once

    later = finish.finish_pair(
        c["directory"],
        producer,
        review,
        home=r["home"],
        guard=no_writes,
        once=_once,
        clock=lambda: 1200,
        command=no_writes,
    )
    assert later == result["reviews"][0]


def test_lost_final_receipt_can_be_reconciled_after_expiry_without_new_board_writes(completion):
    c, r = completion, completion["r"]
    c["run"]()
    (c["directory"] / "finished.json").unlink()
    producer = json.loads((c["directory"] / "candidate-intent.json").read_text())
    review = json.loads((c["directory"] / "review-intent.json").read_text())

    def no_writes(*args, **kwargs):
        raise AssertionError("Historical recovery must not mutate the board")

    from fiber_lifecycle import _once

    result = finish.finish_pair(
        c["directory"],
        producer,
        review,
        home=r["home"],
        guard=no_writes,
        once=_once,
        clock=lambda: 1200,
        command=no_writes,
    )
    assert result["accepted"] is True


@pytest.mark.parametrize("change", ["expired", "missing", "changed-plan", "failed"])
def test_invalid_test_proof_never_records_pass(completion, change):
    c, r = completion, completion["r"]
    receipt_path = c["directory"] / "test-receipt.json"
    if change == "missing":
        receipt_path.unlink()
    elif change == "changed-plan":
        plan_path = c["directory"] / "test-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["checks"][0]["argv"] = ["true"]
        plan_path.write_text(json.dumps(plan))
    elif change == "failed":
        receipt = json.loads(receipt_path.read_text())
        receipt["checks"][0]["failed"] = 1
        receipt_path.write_text(json.dumps(receipt))
    before = {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    if change == "missing":
        assert c["run"]()["reviews"][0]["state"] == "awaiting-required-tests"
    else:
        with pytest.raises(Refused):
            c["run"](**({"clock": lambda: 1200} if change == "expired" else {}))
    assert before == {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}


@pytest.mark.parametrize("replacement", [None, "0" * 32])
def test_lost_reply_retry_refuses_a_removed_review_invocation(completion, replacement):
    """A terminal row without an invocation cannot match an arbitrary live unit."""
    import sqlite3

    c, r = completion, completion["r"]

    def interrupted(home, args):
        c["command"](home, args)
        raise Refused("synthetic lost reply")

    with pytest.raises(Refused, match="lost reply"):
        c["run"](interrupted)
    with sqlite3.connect(r["state"] / "admission.sqlite3") as db:
        db.execute(
            "UPDATE runs SET invocation=? WHERE card=?", (replacement, c["returned"]["review"])
        )
    before = {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    with pytest.raises(Refused, match="unit-changed"):
        c["run"](
            observe=lambda run: json.loads(run["receipt"])
            | (
                {"invocation": replacement or "0" * 32}
                if run["card"] == c["returned"]["review"]
                else {}
            )
        )
    assert before == {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
