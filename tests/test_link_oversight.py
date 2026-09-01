"""Focused checks for the read-only Link oversight projection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from skcapstone.link_oversight import (
    AgeObservation,
    ChurnEvent,
    ClaimObservation,
    GatewayObservation,
    LaneConfig,
    OversightInput,
    ProcessObservation,
    ReviewAssignment,
    append_evidence,
    project,
)


def _source() -> OversightInput:
    return OversightInput(
        observed_at="2026-09-01T12:00:00Z",
        window_start="2026-09-01T11:00:00Z",
        stale_claim_seconds=300,
        max_churn_events=2,
        lanes=(
            LaneConfig("qwen", 1, "config:qwen"),
            LaneConfig("codex", 3, "config:codex"),
            LaneConfig("glm", 2, "config:glm"),
        ),
        processes=(
            ProcessObservation(
                "codex",
                "worker-a",
                "live",
                "2026-09-01T11:59:00Z",
                "process:a",
                "rev-a",
            ),
            ProcessObservation(
                "glm", "worker-b", "live", "2026-09-01T11:58:00Z", "process:b", "rev-b"
            ),
        ),
        churn=(
            ChurnEvent("codex", "launch", "2026-09-01T11:01:00Z", "old", "churn:1"),
            ChurnEvent("codex", "exit", "2026-09-01T11:30:00Z", "old", "churn:2"),
            ChurnEvent("glm", "launch", "2026-09-01T11:45:00Z", "worker-b", "churn:3"),
        ),
        claims=(
            ClaimObservation("card-a", "worker-a", "rev-a", "2026-09-01T11:55:00Z", "cardstore:a"),
            ClaimObservation(
                "card-stale",
                "missing-worker",
                "rev-stale",
                "2026-09-01T11:00:00Z",
                "cardstore:stale",
            ),
        ),
        reviews=(
            ReviewAssignment(
                "review-a",
                "author",
                "chiap01",
                "session-a",
                "/work/a",
                "author",
                "chiap02",
                "session-b",
                "/work/b",
                "review:event-a",
            ),
            ReviewAssignment(
                "review-b",
                "author-b",
                "chiap03",
                "session-c",
                "/work/c",
                "reviewer-b",
                "chiap03",
                "session-d",
                "/work/d",
                "review:event-b",
            ),
        ),
        gateway=(
            GatewayObservation("codex", 10, "2026-09-01T11:10:00Z", "gateway:1"),
            GatewayObservation("codex", 20, "2026-09-01T11:20:00Z", "gateway:2"),
            GatewayObservation("codex", 30, "2026-09-01T11:30:00Z", "gateway:3", "provider_exit"),
        ),
        ages=(
            AgeObservation("pr:1", 601, 600, "2026-09-01T12:00:00Z", "github:pr1"),
            AgeObservation("pr:2", 599, 600, "2026-09-01T12:00:00Z", "github:pr2"),
        ),
    )


def test_projection_is_deterministic_typed_and_complete() -> None:
    snapshot = project(_source())

    assert snapshot == project(_source())
    assert snapshot.schema == "link-oversight/v1"
    assert [
        (lane["lane"], lane["configured_slots"], lane["live_slots"], lane["free_slots"])
        for lane in snapshot.lanes
    ] == [
        ("codex", 3, 1, 2),
        ("glm", 2, 1, 1),
        ("qwen", 1, 0, 1),
    ]
    assert snapshot.churn["truncated"] is True
    assert snapshot.churn["launches"] == snapshot.churn["exits"] == 1
    assert [item["card_id"] for item in snapshot.stale_claim_process_joins] == ["card-stale"]
    assert snapshot.duplicate_review_dimensions == (
        {"card_id": "review-a", "dimensions": ("identity",), "provenance": "review:event-a"},
        {"card_id": "review-b", "dimensions": ("host",), "provenance": "review:event-b"},
    )
    codex = snapshot.gateway[0]
    assert codex["p50_latency_ms"] == 20
    assert codex["p95_latency_ms"] == 29
    assert codex["terminal_errors"][0]["error"] == "provider_exit"
    assert [item["subject"] for item in snapshot.age_threshold_breaches] == ["pr:1"]
    assert {item["kind"] for item in snapshot.recommendations} == {
        "inspect_stale_claim_joins",
        "assign_distinct_reviewer",
        "inspect_gateway_errors",
        "inspect_age_breaches",
    }
    assert "cardstore:stale" in snapshot.provenance


def test_evidence_sink_appends_parseable_serializer_built_json(tmp_path: Path) -> None:
    path = tmp_path / "oversight.jsonl"
    snapshot = project(_source())

    first_hash = append_evidence(path, snapshot)
    second_hash = append_evidence(path, snapshot)
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)

    assert len(lines) == 2
    assert all(json.loads(line)["schema"] == "link-oversight/v1" for line in lines)
    assert first_hash == second_hash == hashlib.sha256(lines[0]).hexdigest()
    assert lines[0] == lines[1]


def test_projection_surface_exposes_no_actuator() -> None:
    import skcapstone.link_oversight as oversight

    forbidden = {"claim", "release", "launch", "stop", "reassign", "merge", "close", "deploy"}
    surface = oversight.__all__ if hasattr(oversight, "__all__") else dir(oversight)
    assert forbidden.isdisjoint(surface)
