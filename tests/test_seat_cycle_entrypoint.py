"""Tests for the fenced Link and Mero recurring entrypoint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from skcapstone.seat_cycle_entrypoint import (
    link_operation,
    load_control_plane,
    run_cycle,
    seraph_operation,
)


def control(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {"link": ["chiap08"], "mero": ["chiap08"], "seraph": ["chiap08"]},
            }
        ),
        encoding="utf-8",
    )


def test_inactive_host_refuses_before_operation(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    called = []
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap01",
        operation=lambda: called.append(True) or {"recommendations": 1},
    )
    assert result.result == "inactive_host_refused"
    assert called == []
    health = (tmp_path / "home/coordination/seat-cycles/link.health.jsonl").read_text()
    assert "inactive_host_refused" in health


def test_dry_run_is_fenced_and_bounded(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    called = []
    result = run_cycle(
        seat="mero",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
        dry_run=True,
        operation=lambda: called.append(True) or {"recommendations": 9},
    )
    assert result.result == "dry_run"
    assert result.recommendations == 0
    assert called == []


def test_active_cycle_records_operation_summary(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
        operation=lambda: {"cards_examined": 4, "recommendations": 2, "suppressed": 1},
    )
    assert result.result == "complete"
    assert (tmp_path / "home/coordination/seat-cycles/link.cycle.receipts.jsonl").exists()
    assert result.cards_examined == 4
    assert result.recommendations == 2


def test_stale_feed_emits_only_signed_lineage_review_work(tmp_path: Path) -> None:
    home = tmp_path / "home"
    lineage = home / "coordination/link-lineage.json"
    lineage.parent.mkdir(parents=True)
    item = {
        "kind": "review-work",
        "reason": "missing_terminal_review",
        "repository": "org/repo",
        "pr": 7,
        "head_revision": "a" * 40,
        "base_revision": "b" * 40,
        "source_card": "source01",
        "card_generation": "2" * 64,
        "source_owner": "builder",
        "reviewer_candidates": [
            {"name": "Seraph", "seat": "seraph", "identity": "seraph", "eligible": True}
        ],
    }
    manifest = {
        "schema": "skfleet.link-lineage/v1",
        "source_revision": "1" * 64,
        "coverage": {"unresolved": 1},
        "records": {},
        "reviewer_candidates": [],
        "diagnostics": [],
        "unresolved_prs": [7],
        "review_work_recommendations": [item],
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest["evidence_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    lineage.write_text(json.dumps(manifest))
    result = link_operation(home, home / "coordination/missing-feed.json", lineage)
    assert result["reason"].startswith("review_work_only:")
    assert result["recommendations"] == 1
    emitted = json.loads(
        (home / "coordination/seat-cycles/link.review-work.jsonl").read_text().strip()
    )
    assert emitted["head_revision"] == "a" * 40
    assert emitted["source_card"] == "source01"
    assert emitted["reconciliation"]["launchable"] is False
    assert emitted["reconciliation"]["reason"] == "source_card_missing"


def test_cycle_without_operation_is_explicit_noop(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
    )
    assert result.result == "operation_not_configured"
    assert result.reason == "operation_not_configured"


def test_seraph_dispatch_is_exactly_one_and_seat_scoped(tmp_path, monkeypatch) -> None:
    captured = {}

    def run(command, **kwargs):
        captured.update(kwargs["env"])
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    result = seraph_operation(tmp_path)
    assert result["reason"] == "seraph_dispatch_complete"
    assert captured["SKFLEET_ONLY_SEAT"] == "seraph"
    assert captured["SKFLEET_MAX_LAUNCH"] == "1"
    assert captured["SKFLEET_TARGET"] == "1"


def test_control_plane_requires_both_seats(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {"link": ["chiap08"]},
            }
        ),
        encoding="utf-8",
    )
    try:
        load_control_plane(path)
    except ValueError as exc:
        assert "mero" in str(exc) or "seraph" in str(exc)
    else:
        raise AssertionError("incomplete control plane accepted")


def test_unit_templates_preserve_limits_and_disabled_install_contract() -> None:
    root = Path(__file__).parents[1]
    link = (root / "systemd/skfleet-link.service").read_text()
    mero = (root / "systemd/skfleet-mero.service").read_text()
    link_timer = (root / "systemd/skfleet-link.timer").read_text()
    mero_timer = (root / "systemd/skfleet-mero.timer").read_text()
    seraph = (root / "systemd/skfleet-seraph.service").read_text()
    seraph_timer = (root / "systemd/skfleet-seraph.timer").read_text()
    assert "TimeoutStartSec=120" in link
    assert "TimeoutStartSec=180" in mero
    assert "--seat link" in link and "--seat mero" in mero
    assert "OnUnitActiveSec=5min" in link_timer
    assert "OnUnitActiveSec=10min" in mero_timer
    assert "skfleet-mero.service" in mero_timer
    assert "TimeoutStartSec=300" in seraph
    assert "--seat seraph" in seraph
    assert "SKFLEET_MAX_LAUNCH" not in seraph
    assert "skfleet-seraph.service" in seraph_timer
