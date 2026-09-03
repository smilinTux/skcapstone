"""Transport failures before agent work do not consume card attempts."""

from __future__ import annotations

import ast
import json
import os
import re
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {
    "_card_mutated_during_report",
    "_structured_transport_failure",
    "_is_substantive_worker_report",
    "_launch_epoch_from_log",
    "_latest_transport_failure_epoch",
    "_local_launch_evidence",
    "_reporting_launches",
    "_transport_retry_held",
    "launch_attempts",
}
CONSTANTS = {"_LAUNCH_TTL_H", "_LOGDIR", "_TRANSPORT_RETRY_COOLDOWN_S", "_GATEWAY_ERROR_RE"}


def _namespace() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & CONSTANTS:
                nodes.append(node)
    namespace = {
        "datetime": __import__("datetime"),
        "json": json,
        "os": os,
        "re": re,
        "time": time,
        "HOME": "/unused",
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert FUNCTIONS <= namespace.keys()
    return namespace


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ('404 {"message":"","code":404}', "gateway_404"),
        ('429: {"message":"backend cooldown","code":429}', "rate_limited"),
        ('400: {"message":"The \'glm-4.6\' model is not supported when using Codex with a ChatGPT account.","code":400}', "unsupported_model_route"),
        ('503: {"message":"backend unavailable","code":503,"type":"model_owner_backend_down"}', "model_owner_backend_down"),
        ('503: {"message":"claims quarantined","code":503,"type":"model_claim_quarantined"}', "backend_claims_quarantined"),
        ("Connection error.", "connection_failure"),
        (
            '504: {"message":"timed out before first token",'
            '"code":"timeout_before_first_token"}',
            "first_token_timeout",
        ),
        (
            '502: {"message":"bad tool evidence",'
            '"code":"invalid_upstream_tool_calls"}',
            "invalid_upstream_tool_calls",
        ),
    ],
)
def test_exact_structured_pre_agent_failures(line: str, kind: str) -> None:
    assert _namespace()["_structured_transport_failure"](line) == kind


@pytest.mark.parametrize(
    "text",
    [
        "worker discussed a 429 cooldown",
        '429: {"message":"cooldown","code":429}\nagent produced analysis',
        '200: {"message":"ok","code":200}',
        '502: {"message":"other upstream error","code":"upstream_error"}',
    ],
)
def test_arbitrary_or_mixed_output_is_substantive(text: str) -> None:
    namespace = _namespace()
    assert namespace["_structured_transport_failure"](text) is None
    assert namespace["_is_substantive_worker_report"](text, False) is True


def test_card_mutation_makes_transport_output_substantive() -> None:
    namespace = _namespace()
    text = '404: {"message":"gateway route unavailable","code":404}'
    assert namespace["_is_substantive_worker_report"](text, False) is False
    assert namespace["_is_substantive_worker_report"](text, True) is True


def test_three_real_attempts_remain_three() -> None:
    namespace = _namespace()
    reports = ["analysis complete", "BLOCKED with evidence", "PASS_FOR_REVIEW"]
    assert sum(namespace["_is_substantive_worker_report"](text, False) for text in reports) == 3


def test_one_recovery_probe_after_bounded_cooldown() -> None:
    namespace = _namespace()
    namespace["_latest_transport_failure_epoch"] = lambda _cid: 100.0
    namespace["time"] = type("Clock", (), {"time": staticmethod(lambda: 159.0)})
    assert namespace["_transport_retry_held"]("deadbeef") is True
    namespace["time"] = type("Clock", (), {"time": staticmethod(lambda: 160.0)})
    assert namespace["_transport_retry_held"]("deadbeef") is False


def test_transport_log_overrides_shared_launch_receipt(tmp_path: Path) -> None:
    namespace = _namespace()
    card = "deadbeef"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    (tmp_path / f"{card}-{stamp}.log").write_text(
        '404: {"message":"gateway route unavailable","code":404}',
        encoding="utf-8",
    )
    namespace.update(
        {
            "_LAUNCH_TTL_H": 6,
            "_LOGDIR": str(tmp_path),
            "_shared_launch_attempts": lambda _cid: 1,
            "event_rows": lambda _cid: [],
            "_ts_epoch": lambda event_stamp: float(event_stamp or 0),
        }
    )
    assert namespace["_local_launch_evidence"](card)[:2] == (1, 0)
    assert namespace["launch_attempts"](card) == 0


def test_mixed_output_and_card_mutation_each_consume_attempt(tmp_path: Path) -> None:
    namespace = _namespace()
    card = "feedface"
    now = time.time()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    log = tmp_path / f"{card}-{stamp}.log"
    log.write_text(
        '429: {"message":"backend cooldown","code":429}\nagent output',
        encoding="utf-8",
    )
    namespace.update(
        {
            "_LAUNCH_TTL_H": 6,
            "_LOGDIR": str(tmp_path),
            "_shared_launch_attempts": lambda _cid: 1,
            "event_rows": lambda _cid: [],
            "_ts_epoch": lambda event_stamp: float(event_stamp or 0),
        }
    )
    assert namespace["launch_attempts"](card) == 1

    log.write_text('429: {"message":"backend cooldown","code":429}', encoding="utf-8")
    namespace["event_rows"] = lambda _cid: [
        {"ts": now, "action": "link", "writer": "pi-codex-example"}
    ]
    assert namespace["launch_attempts"](card) == 1
