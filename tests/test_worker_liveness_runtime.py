"""Adversarial and production adapter tests for worker liveness."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import worker_liveness_runtime as runtime
from skcapstone.fleet.worker_liveness import (
    LivenessObservation,
    WorkerProjection,
    _retirement_receipt,
)

NOW = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[1]


def observation(tmp_path: Path, **changes: object) -> LivenessObservation:
    """Build complete synthetic evidence for one exact worker generation."""
    values: dict[str, object] = {
        "host": "chiap08",
        "observer_host": "chiap08",
        "observed_at": NOW,
        "owner": "worker",
        "card_id": "deadbeef",
        "claim_generation": "generation-1",
        "process_identity": "pid:123",
        "session_id": "codex-auto-deadbeef",
        "managed_session": True,
        "process_alive": False,
        "session_alive": False,
        "live_children": 0,
        "child_activity_at": None,
        "heartbeat_at": NOW,
        "terminal_marker": "PASS_FOR_REVIEW",
        "terminal_at": NOW,
        "skmail_response_at": None,
        "assistance_requested_at": None,
        "workspace_recoverable": True,
        "workspace_custody": "preserved",
        "current_claim_generation": "generation-1",
        "cgroup_processes": 0,
        "unit": "skfleet-worker-codex-deadbeef.service",
        "pid": 123,
        "process_tree": (123,),
        "cgroup": "/user.slice/skfleet-worker-codex-deadbeef.service",
        "process_observed_at": NOW,
        "cgroup_observed_at": NOW,
        "beat_id": "beat-1",
        "workspace_path": str(tmp_path.resolve()),
        "workspace_repository": "https://example.test/skcapstone.git",
        "workspace_head": "a" * 40,
        "workspace_custody_at": NOW,
        "workspace_custody_sha256": "b" * 64,
    }
    values.update(changes)
    return LivenessObservation(**values)  # type: ignore[arg-type]


def authoritative_fixture(tmp_path: Path, row: LivenessObservation) -> None:
    """Write the host owned beat matched by the authority validator."""
    path = tmp_path / "fleet" / "beats" / f"{row.owner}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "beat_id": row.beat_id,
                "host": row.host,
                "unit": row.unit,
                "pid": row.pid,
                "process_identity": row.process_identity,
                "process_tree": list(row.process_tree),
                "cgroup": row.cgroup,
                "card_id": row.card_id,
                "claim_revision": row.claim_generation,
                "terminal_marker": row.terminal_marker,
                "workspace_path": row.workspace_path,
                "workspace_repository": row.workspace_repository,
                "workspace_head": row.workspace_head,
                "workspace_custody_sha256": row.workspace_custody_sha256,
            }
        ),
        encoding="utf-8",
    )


def authoritative_cgroup(tmp_path: Path, row: LivenessObservation, members: str = "") -> Path:
    """Write the final host-owned cgroup membership authority."""
    path = tmp_path / str(row.cgroup).lstrip("/") / "cgroup.procs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(members, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 999},
        {"process_identity": "pid:999"},
        {"process_tree": (123, 999)},
        {"cgroup": "/forged/skfleet-worker-codex-deadbeef.service"},
        {"beat_id": "stale-beat"},
        {"terminal_marker": "PASS"},
        {"workspace_path": "/missing"},
        {"workspace_repository": "https://evil.test/repo.git"},
        {"workspace_head": "c" * 40},
        {"workspace_custody_sha256": "d" * 64},
        {"host": "chiap01", "observer_host": "chiap01"},
        {"unit": "skfleet-worker-codex-feedface.service"},
        {"card_id": "feedface"},
        {"claim_generation": "stale-generation"},
    ],
)
def test_each_mismatched_authority_dimension_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    """Reject forged PID, tree, cgroup, beat, terminal, workspace, and claim facts."""
    trusted = observation(tmp_path)
    authoritative_fixture(tmp_path, trusted)
    authoritative_cgroup(tmp_path, trusted)
    monkeypatch.setattr(runtime, "_claim_revision", lambda *_: "generation-1")
    monkeypatch.setattr(
        runtime,
        "workspace_custody",
        lambda *_: (
            trusted.workspace_repository,
            trusted.workspace_head,
            trusted.workspace_custody_sha256,
        ),
    )

    def runner(argv: list[str]) -> SimpleNamespace:
        if "show" in argv:
            body = (
                f"Id={trusted.unit}\nExecMainPID={trusted.pid}\n"
                f"ControlGroup={trusted.cgroup}\nActiveState=inactive\n"
            )
            return SimpleNamespace(returncode=0, stdout=body)
        return SimpleNamespace(returncode=3, stdout=trusted.cgroup)

    assert not runtime.authorize_observation(
        replace(trusted, **change),
        home=tmp_path,
        runner=runner,
        hostname="chiap08",
        cgroup_root=tmp_path,
    )


def test_final_authorization_freshly_rereads_empty_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authorize the valid path only while the final cgroup read stays empty."""
    row = observation(tmp_path)
    authoritative_fixture(tmp_path, row)
    authoritative_cgroup(tmp_path, row)
    monkeypatch.setattr(runtime, "_claim_revision", lambda *_: row.claim_generation)
    monkeypatch.setattr(
        runtime,
        "workspace_custody",
        lambda *_: (row.workspace_repository, row.workspace_head, row.workspace_custody_sha256),
    )

    def runner(argv: list[str]) -> SimpleNamespace:
        if "show" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"Id={row.unit}\nExecMainPID={row.pid}\n"
                    f"ControlGroup={row.cgroup}\nActiveState=inactive\n"
                ),
            )
        return SimpleNamespace(returncode=3, stdout=str(row.cgroup))

    assert runtime.authorize_observation(
        row, home=tmp_path, runner=runner, hostname=row.host, cgroup_root=tmp_path
    )


@pytest.mark.parametrize("members", ["123\n", "999\n", "123\n999\n", "not-a-pid\n"])
def test_final_authorization_rejects_cgroup_toctou(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    members: str,
) -> None:
    """Reject membership appearing or becoming malformed after classification."""
    row = observation(tmp_path)
    authoritative_fixture(tmp_path, row)
    authoritative_cgroup(tmp_path, row, members)
    monkeypatch.setattr(runtime, "_claim_revision", lambda *_: row.claim_generation)
    monkeypatch.setattr(
        runtime,
        "workspace_custody",
        lambda *_: (row.workspace_repository, row.workspace_head, row.workspace_custody_sha256),
    )

    def runner(argv: list[str]) -> SimpleNamespace:
        if "show" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"Id={row.unit}\nExecMainPID={row.pid}\n"
                    f"ControlGroup={row.cgroup}\nActiveState=inactive\n"
                ),
            )
        return SimpleNamespace(returncode=3, stdout=str(row.cgroup))

    assert not runtime.authorize_observation(
        row, home=tmp_path, runner=runner, hostname=row.host, cgroup_root=tmp_path
    )


def test_final_authorization_rejects_unreadable_or_missing_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = observation(tmp_path)
    authoritative_fixture(tmp_path, row)
    monkeypatch.setattr(runtime, "_claim_revision", lambda *_: row.claim_generation)
    monkeypatch.setattr(
        runtime,
        "workspace_custody",
        lambda *_: (row.workspace_repository, row.workspace_head, row.workspace_custody_sha256),
    )

    def runner(argv: list[str]) -> SimpleNamespace:
        if "show" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"Id={row.unit}\nExecMainPID={row.pid}\n"
                    f"ControlGroup={row.cgroup}\nActiveState=inactive\n"
                ),
            )
        return SimpleNamespace(returncode=3, stdout=str(row.cgroup))

    assert not runtime.authorize_observation(
        row, home=tmp_path, runner=runner, hostname=row.host, cgroup_root=tmp_path
    )


def test_retire_rereads_cgroup_immediately_before_actuation(tmp_path: Path) -> None:
    """Catch membership that appears after the decision but before stop."""
    row = observation(tmp_path)
    process_file = authoritative_cgroup(tmp_path, row)
    commands: list[list[str]] = []
    actions = runtime.ProductionActions(
        tmp_path,
        "producer",
        lambda argv: commands.append(argv) or SimpleNamespace(returncode=0, stdout=""),
        cgroup_root=tmp_path,
    )
    receipt = _retirement_receipt(row)
    assert receipt is not None

    process_file.write_text("456\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="authority changed"):
        actions.retire(receipt)
    assert not any(command[-2:] == ["stop", row.unit] for command in commands)


def test_retire_valid_positive_path_stops_exact_unit(tmp_path: Path) -> None:
    row = observation(tmp_path)
    authoritative_cgroup(tmp_path, row)
    commands: list[list[str]] = []
    actions = runtime.ProductionActions(
        tmp_path,
        "producer",
        lambda argv: commands.append(argv) or SimpleNamespace(returncode=0, stdout=""),
        cgroup_root=tmp_path,
    )
    receipt = _retirement_receipt(row)
    assert receipt is not None

    actions.retire(receipt)
    assert commands == [["systemctl", "--user", "stop", row.unit]]


def test_production_cycle_invokes_all_four_real_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive assistance, reconciliation, metrics, and retirement adapters together."""
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> SimpleNamespace:
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="")

    class Actions(runtime.ProductionActions):
        def __init__(self, home: Path, agent: str):
            super().__init__(home, agent, runner, cgroup_root=tmp_path)

    monkeypatch.setattr(runtime, "authorize_observation", lambda *_args, **_kwargs: True)
    terminal = observation(tmp_path)
    authoritative_cgroup(tmp_path, terminal)
    assistance = replace(
        observation(tmp_path, card_id="feedface", claim_generation="generation-2"),
        terminal_marker=None,
        terminal_at=None,
        process_alive=True,
        session_alive=True,
        heartbeat_at=NOW - timedelta(hours=2),
        current_claim_generation="generation-2",
        unit="skfleet-worker-codex-feedface.service",
        cgroup="/user.slice/skfleet-worker-codex-feedface.service",
    )
    result = runtime.run_production_cycle(
        tmp_path,
        observations=(terminal, assistance),
        projections=(WorkerProjection("worker", "deadbeef", "generation-1", "active"),),
        agent="producer",
        actions_factory=Actions,
        now=NOW,
    )
    assert result.receipts
    assert any("work.help.request" in command for command in commands)
    assert any("worker_liveness" in command for command in commands)
    assert any(command[-2:] == ["stop", terminal.unit] for command in commands)
    assert (tmp_path / "evidence" / "fleet-liveness" / "metrics.json").is_file()
    assert (tmp_path / "evidence" / "fleet-liveness" / "retirements.sqlite3").is_file()


def test_timer_entrypoint_uses_relocated_fleet_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove the real timer call delegates root selection to FleetPaths."""
    fleet_root = tmp_path / "relocated" / "fleet"
    monkeypatch.setenv("SKFLEET_ROOT", str(fleet_root))
    captured: list[Path] = []

    class Actions(runtime.ProductionActions):
        def __init__(self, home: Path, agent: str):
            captured.append(home)
            super().__init__(home, agent, lambda _: SimpleNamespace(returncode=0, stdout=""))

    runtime.run_production_cycle(observations=(), projections=(), actions_factory=Actions, now=NOW)
    assert captured == [fleet_root.parent]
    assert (fleet_root.parent / "evidence" / "fleet-liveness" / "metrics.json").is_file()

    tree = ast.parse((ROOT / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_production_cycle"
    ]
    assert len(calls) == 1
    assert calls[0].args == []
