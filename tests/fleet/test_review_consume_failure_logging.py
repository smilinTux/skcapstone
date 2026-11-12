"""A refused remote review request on a builder node is logged once, not swallowed."""

from __future__ import annotations

import logging

from skcapstone.fleet import builder_dispatch


def _reset():
    builder_dispatch._REVIEW_CONSUME_FAILURES_LOGGED.clear()


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
