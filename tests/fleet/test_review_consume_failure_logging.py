"""A refused remote review request on a builder node is logged once, not swallowed."""

from __future__ import annotations

import json
import logging

from skcapstone.fleet import builder_dispatch
from skcapstone.fleet.paths import FleetPaths


def _reset():
    builder_dispatch._REVIEW_CONSUME_FAILURES_LOGGED.clear()
    builder_dispatch._REVIEW_POLICY_DRIFT_LOGGED.clear()


def test_refusal_is_logged_with_card_request_and_reason(caplog):
    _reset()
    with caplog.at_level(logging.WARNING, logger=builder_dispatch.__name__):
        builder_dispatch._log_review_consume_failure(
            "node-chiap03",
            {"card_id": "8f4b04d4", "request_id": "bacb60df" + "0" * 56},
            ValueError("remote review offer expired"),
        )
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert msg.startswith("REVIEW_CONSUME_REFUSED|node-chiap03|8f4b04d4|request=bacb60df")
    assert "ValueError: remote review offer expired" in msg


def test_same_request_and_reason_is_logged_once(caplog):
    _reset()
    req = {"card_id": "8f4b04d4", "request_id": "r1"}
    with caplog.at_level(logging.WARNING, logger=builder_dispatch.__name__):
        for _ in range(5):
            builder_dispatch._log_review_consume_failure("n", req, ValueError("x"))
    assert len(caplog.records) == 1


def test_a_new_reason_for_the_same_request_is_logged(caplog):
    _reset()
    req = {"card_id": "8f4b04d4", "request_id": "r1"}
    with caplog.at_level(logging.WARNING, logger=builder_dispatch.__name__):
        builder_dispatch._log_review_consume_failure("n", req, ValueError("first"))
        builder_dispatch._log_review_consume_failure("n", req, OSError("second"))
    assert [r.getMessage().rsplit("|", 1)[-1] for r in caplog.records] == [
        "ValueError: first",
        "OSError: second",
    ]


def test_policy_drift_holds_stale_review_and_logs_once(tmp_path, monkeypatch, caplog):
    _reset()
    node = "node-chiap03"
    paths = FleetPaths(tmp_path / "fleet")
    directory = paths.root / "dispatch" / node
    directory.mkdir(parents=True)
    request = {
        "schema": "skfleet.builder-dispatch/v2",
        "node": node,
        "card_id": "8f4b04d4",
        "request_id": "a" * 64,
        "policy": {"lanes": {"codex": {"enabled": True}}},
    }
    request_path = directory / "8f4b04d4.json"
    original = json.dumps(request).encode()
    request_path.write_bytes(original)
    current = {"lanes": {"codex": {"enabled": False}}}
    monkeypatch.setattr(builder_dispatch.production_builder, "policy", lambda: current)
    monkeypatch.setattr(
        builder_dispatch.store,
        "read_spec",
        lambda *_: {"spec": {"role": builder_dispatch.ROLE, "actuate": True}},
    )
    monkeypatch.setattr(builder_dispatch.store, "actuation_allowed", lambda *_: True)
    monkeypatch.setattr(builder_dispatch, "_dispatch_statuses", lambda *_: {})

    def consume(*_args, **_kwargs):
        raise AssertionError("stale review policy must be held before consumption")

    from skcapstone.fleet import review_dispatch

    monkeypatch.setattr(review_dispatch, "consume_review", consume)
    with caplog.at_level(logging.WARNING, logger=builder_dispatch.__name__):
        builder_dispatch._consume_available(paths, tmp_path, node, None, lambda *_: None)
        builder_dispatch._consume_available(paths, tmp_path, node, None, lambda *_: None)

    policy_logs = [record.getMessage() for record in caplog.records]
    assert len(policy_logs) == 1
    assert policy_logs[0].startswith(
        "POLICY_DRIFT_HELD|node-chiap03|8f4b04d4|request=aaaaaaaaaaaaaaaa|"
    )
    assert request_path.read_bytes() == original
    assert not paths.status_path(node, "dispatch", "8f4b04d4").exists()
