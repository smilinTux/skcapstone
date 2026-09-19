"""Gateway failures the fleet actually emits are recognised as pre-agent.

Every body in REAL_BODIES is copied verbatim from a worker-exit record under
``~/.skcapstone/evidence/fleet-worker-exits`` on the chi fleet, read
2026-09-18. Measured over all 2,980 records that day, 2,202 carried a gateway
status+JSON body and the shipped classifier recognised 133 of them (6.0%).
1,793 were a 503, which neither the launcher's ``_GATEWAY_ERROR_RE`` (status
set 400/404/408/429/502/504) nor the wrapper's ``TRANSPORT_PATTERNS``
matched, so a pure infrastructure outage was scored as failed work on the
card and re-dispatched every five minutes.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import time
from pathlib import Path

import pytest

from skcapstone.fleet import gateway_failure
from skcapstone.fleet.gateway_failure import (
    TRANSPORT_FAILURE_CLASSES,
    classify_gateway_failure,
    classify_transport_diagnostic,
)

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
WRAPPER = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"


def _wrapper():
    spec = importlib.util.spec_from_file_location("skfleet_worker_wrapper", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _launcher_classifier():
    """Extract the launcher's classifier exactly as the deployed script runs it."""
    wanted = {"_structured_transport_failure", "_is_substantive_worker_report"}
    nodes = [
        node
        for node in ast.parse(ROTATE.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {
        "json": json,
        "os": os,
        "re": re,
        "time": time,
        "classify_gateway_failure": gateway_failure.classify_gateway_failure,
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


#: (body, expected kind). Verbatim gateway output, with counts as measured.
REAL_BODIES = [
    # 1,317 records. The glm lane's buckets have no eligible member because
    # every GLM model is lifecycle-retired and the bucket is pinned to zai.
    (
        '503: {"message":"No model satisfies bucket sk-glm-s (capability floor S, '
        'max trust zone 2).","code":503,"type":"bucket_no_eligible_member",'
        '"bucket":"sk-glm-s","model_class":"S","sensitivity":"public","ceiling":2}',
        "bucket_no_eligible_member",
    ),
    # 474 records, and invisible until now: the wrapper matched the spelling
    # "backend-claims-quarantined", which the gateway has never emitted.
    (
        '503: {"message":"all backend claims for model \\"glm-4.6\\" are quarantined",'
        '"code":503,"type":"model_claim_quarantined","model":"glm-4.6","retryable":true}',
        "backend_claims_quarantined",
    ),
    # 13 records. Explicitly retryable: the queue was full, the card is fine.
    (
        '503: {"message":"Capacity domain chiap01-qwen38 queue wait timed out.",'
        '"code":"queue_timeout","backend":"chiap01-qwen38","retryable":true,'
        '"retry_after_seconds":30}',
        "capacity_exhausted",
    ),
    # 5 records.
    (
        '503: {"message":"Capacity domain chiap01-qwen38 queue is full.",'
        '"code":"capacity_exceeded","backend":"chiap01-qwen38","retryable":true,'
        '"retry_after_seconds":30}',
        "capacity_exhausted",
    ),
    # 70 records.
    (
        '502: {"message":"Upstream returned no visible content or tool calls",'
        '"code":"empty_upstream_response","type":"upstream_error"}',
        "upstream_failure",
    ),
    # 54 records.
    (
        '502: {"message":"Upstream returned invalid completion evidence",'
        '"code":"invalid_upstream_completion","type":"upstream_error"}',
        "upstream_failure",
    ),
    # 6 records.
    (
        '502: {"message":"socket hang up","code":"upstream_unreachable"}',
        "upstream_failure",
    ),
    # 9 records.
    (
        '404: {"message":"The model `qwen3.8-27b` does not exist.",'
        '"type":"NotFoundError","param":"model","code":404}',
        "gateway_404",
    ),
]


@pytest.mark.parametrize(("body", "kind"), REAL_BODIES, ids=[k for _, k in REAL_BODIES])
def test_real_gateway_bodies_are_pre_agent_failures(body: str, kind: str) -> None:
    assert classify_gateway_failure(body) == kind


@pytest.mark.parametrize("body", [body for body, _ in REAL_BODIES])
def test_every_recognised_kind_is_a_valid_exit_record_class(body: str) -> None:
    """The launcher filters worker-exit records on TRANSPORT_FAILURE_CLASSES.

    A kind outside that set would be stamped into the record and then silently
    ignored by the launcher, which is the split-brain in a second costume.
    """
    assert classify_transport_diagnostic(body) in TRANSPORT_FAILURE_CLASSES


@pytest.mark.parametrize(("body", "kind"), REAL_BODIES, ids=[k for _, k in REAL_BODIES])
def test_launcher_and_wrapper_agree_on_every_real_body(body: str, kind: str) -> None:
    """Neither side may recognise a failure the other misses.

    The nine-hour churn on seat pi-glm-chiap01-0aec5a64 ran because the two
    classifiers were separate lists maintained by hand.
    """
    launcher = _launcher_classifier()["_structured_transport_failure"](body)
    wrapper = _wrapper().classify_transport_failure(body)
    assert launcher is not None
    assert wrapper is not None
    assert wrapper in TRANSPORT_FAILURE_CLASSES


@pytest.mark.parametrize("body", [body for body, _ in REAL_BODIES])
def test_a_gateway_failure_is_not_a_substantive_worker_report(body: str) -> None:
    """Not substantive means the card is not charged for the gateway's outage."""
    substantive = _launcher_classifier()["_is_substantive_worker_report"]
    assert substantive(body, False) is False
    # Fails closed the moment the worker actually touched the card.
    assert substantive(body, True) is True


def test_worker_cli_preamble_does_not_hide_a_gateway_failure() -> None:
    """86 records carry a CLI advisory ahead of the real body."""
    body = (
        'Warning: Model "kimi-for-coding" not found for provider "skgateway". '
        "Using custom model id.\n"
        '502: {"message":"Upstream returned invalid completion evidence",'
        '"code":"invalid_upstream_completion","type":"upstream_error"}'
    )
    assert classify_gateway_failure(body) == "upstream_failure"


@pytest.mark.parametrize(
    "text",
    [
        # Deliberate fences, preserved from tests/test_skfleet_transport_retry.py.
        # A generic 400 can be the agent's own bad request and must keep
        # charging the card; 61 records in the corpus are a 400 and stay
        # substantive on purpose.
        '400: {"message":"bad request","code":400}',
        '400: {"message":"tool schema invalid","code":"invalid_tools"}',
        '502: {"message":"other upstream error","code":"upstream_error"}',
        '200: {"message":"ok","code":200}',
        # A gateway error AFTER agent output is not a pre-agent failure.
        '503: {"message":"x","code":503,"type":"bucket_no_eligible_member"}\nanalysis',
        "worker discussed a 503 outage",
    ],
)
def test_unrecognised_and_mixed_output_still_charges_the_card(text: str) -> None:
    assert classify_gateway_failure(text) is None
    assert _launcher_classifier()["_is_substantive_worker_report"](text, False) is True


def test_wrapper_classes_still_match_the_launcher_exactly() -> None:
    assert frozenset(_wrapper().TRANSPORT_PATTERNS) == TRANSPORT_FAILURE_CLASSES
