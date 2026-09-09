"""Adversarial and production adapter tests for worker liveness."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import worker_liveness_runtime as runtime
from skcapstone.fleet.worker_liveness import LivenessObservation, WorkerProjection

NOW = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)


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
        replace(trusted, **change), home=tmp_path, runner=runner, hostname="chiap08"
    )


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
            super().__init__(home, agent, runner)

    monkeypatch.setattr(runtime, "authorize_observation", lambda *_args, **_kwargs: True)
    terminal = observation(tmp_path)
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
