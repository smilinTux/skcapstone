"""Focused CLI and MCP receipt adapter checks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.mcp_tools import coord_card_tools


def _event() -> dict:
    return {
        "action": "link",
        "card_id": "card1",
        "event_id": "event1",
        "link_key": "pr",
        "link_value": "https://example.test/1",
        "transition_id": "transition1",
        "writer": "agent1",
    }


@pytest.mark.parametrize("key,value", [("", "x"), (" ", "x"), ("x", ""), ("x", " \t")])
def test_cli_link_rejects_blank_payload_before_write(tmp_path: Path, monkeypatch, key, value):
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("skcoord.graph_truth.write_verified_annotation", forbidden)
    result = CliRunner().invoke(
        main, ["coord", "link", "card1", key, value, "--home", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "must not be blank" in result.output
    assert not called


def test_cli_link_prints_only_verified_receipt(tmp_path: Path, monkeypatch):
    event = _event()
    monkeypatch.setattr(
        "skcoord.graph_truth.write_verified_annotation", lambda *args, **kwargs: event
    )
    result = CliRunner().invoke(
        main,
        ["coord", "link", "card1", "pr", event["link_value"], "--home", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == event


@pytest.mark.parametrize("error", [ValueError("unknown card"), RuntimeError("readback failed")])
def test_cli_link_never_reports_success_on_primitive_failure(tmp_path: Path, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("skcoord.graph_truth.write_verified_annotation", fail)
    result = CliRunner().invoke(
        main, ["coord", "link", "missing", "pr", "value", "--home", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "Linked" not in result.output


@pytest.mark.asyncio
async def test_mcp_link_has_exact_cli_primitive_semantics(tmp_path: Path, monkeypatch):
    event = _event()
    calls = []
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)

    def write(*args, **kwargs):
        calls.append((args, kwargs))
        return event

    monkeypatch.setattr("skcoord.graph_truth.write_verified_annotation", write)
    result = await coord_card_tools._handle_coord_link(
        {"task_id": "card1", "key": "pr", "value": event["link_value"], "agent": "agent1"}
    )
    assert json.loads(result[0].text) == event
    assert calls == [
        (
            (tmp_path, "card1", "link", "agent1"),
            {"link_key": "pr", "link_value": event["link_value"]},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "  "])
async def test_mcp_link_rejects_empty_and_whitespace_values(tmp_path: Path, monkeypatch, value):
    monkeypatch.setattr(coord_card_tools, "_shared_root", lambda: tmp_path)
    result = await coord_card_tools._handle_coord_link(
        {"task_id": "card1", "key": "pr", "value": value}
    )
    assert "error" in json.loads(result[0].text)


def test_review_result_cli_calls_only_reviewed_operation_and_prints_receipt(
    tmp_path: Path, monkeypatch
):
    calls = []
    receipt = SimpleNamespace(
        event_id="event1",
        transition_id="review-result:review1:rev1",
        replayed=True,
        notification_errors=(),
    )

    def record(home, **kwargs):
        calls.append((home, kwargs))
        return receipt

    monkeypatch.setattr("skcoord.record_review_result", record)
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "review-result",
            "review1",
            "parent1",
            "writer1",
            "rev1",
            "PASS",
            "file:///tmp/evidence.json",
            "a" * 64,
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == {
        **receipt.__dict__,
        "notification_errors": [],
    }
    assert calls == [
        (
            tmp_path,
            {
                "review_card_id": "review1",
                "parent_card_id": "parent1",
                "reviewer_identity": "writer1",
                "claim_revision": "rev1",
                "verdict": "PASS",
                "evidence_uri": "file:///tmp/evidence.json",
                "evidence_sha256": "a" * 64,
                "blocked_on": None,
                "blocked_referent": None,
            },
        )
    ]


def test_review_result_cli_passes_exact_blocked_referent(tmp_path: Path, monkeypatch):
    seen = {}

    def record(home, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            event_id="e", transition_id="t", replayed=False, notification_errors=()
        )

    monkeypatch.setattr("skcoord.record_review_result", record)
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "review-result",
            "review1",
            "parent1",
            "writer1",
            "rev1",
            "BLOCKED",
            "file:///tmp/evidence.json",
            "b" * 64,
            "--blocked-on",
            "dependency",
            "--blocked-referent",
            "card:dep1",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert seen["blocked_on"] == "dependency"
    assert seen["blocked_referent"] == "card:dep1"
