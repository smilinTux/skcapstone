"""Tests for the fenced Link and Mero recurring entrypoint."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.seat_cycle_entrypoint import load_control_plane, run_cycle


def control(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {"link": ["chiap08"], "mero": ["chiap08"]},
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
        assert "mero" in str(exc)
    else:
        raise AssertionError("incomplete control plane accepted")


def test_unit_templates_preserve_limits_and_disabled_install_contract() -> None:
    root = Path(__file__).parents[1]
    link = (root / "systemd/skfleet-link.service").read_text()
    mero = (root / "systemd/skfleet-mero.service").read_text()
    link_timer = (root / "systemd/skfleet-link.timer").read_text()
    mero_timer = (root / "systemd/skfleet-mero.timer").read_text()
    assert "TimeoutStartSec=120" in link
    assert "TimeoutStartSec=180" in mero
    assert "--seat link" in link and "--seat mero" in mero
    assert "OnUnitActiveSec=5min" in link_timer
    assert "OnUnitActiveSec=10min" in mero_timer
    assert "skfleet-mero.service" in mero_timer
