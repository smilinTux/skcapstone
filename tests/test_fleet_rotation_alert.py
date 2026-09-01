from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from skcapstone.fleet.cli import fleet
from skcapstone.fleet.rotation_alert import (
    FAILURE_THRESHOLD,
    STALL_THRESHOLD,
    AlertState,
    extract_error,
    install_units,
    is_silent_stall,
    observe,
)


class FakeITIL:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.created: list[dict] = []
        self.updated: list[tuple] = []

    def create_incident(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="inc-rotation")

    def update_incident(self, *args, **kwargs):
        self.updated.append((args, kwargs))


def _runner(calls: list[list[str]]):
    def run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    return run


def test_failure_alert_waits_for_two_cycles_and_carries_error(tmp_path: Path) -> None:
    managers: list[FakeITIL] = []
    calls: list[list[str]] = []

    def factory(home: Path) -> FakeITIL:
        manager = FakeITIL(home)
        managers.append(manager)
        return manager

    state = tmp_path / "state" / "alert.json"
    first = observe(
        "failure",
        "Traceback line\nAttributeError: bad CardStore event",
        invocation="one",
        state_path=state,
        shared_root=tmp_path / "shared",
        host="chiap08",
        manager_factory=factory,
        runner=_runner(calls),
    )
    assert FAILURE_THRESHOLD == 2
    assert first == "pending"
    assert not managers
    assert not calls

    second = observe(
        "failure",
        "Traceback line\nAttributeError: bad CardStore event",
        invocation="two",
        state_path=state,
        shared_root=tmp_path / "shared",
        host="chiap08",
        manager_factory=factory,
        runner=_runner(calls),
    )
    assert second == "alerted"
    assert managers[0].created[0]["severity"] == "sev2"
    assert managers[0].created[0]["failure_class"] == "rotation-dispatch"
    mail_calls = [call for call in calls if call[:2] == ["skmail", "send"]]
    assert {call[3] for call in mail_calls} == {"jarvis", "lumina"}
    assert all("AttributeError: bad CardStore event" in call[-1] for call in mail_calls)
    assert state.stat().st_mode & 0o777 == 0o600


def test_duplicate_invocation_does_not_increment_or_repeat_alert(tmp_path: Path) -> None:
    state_path = tmp_path / "alert.json"
    state = AlertState(consecutive_failures=1, last_invocation="same")
    state.save(state_path)
    result = observe(
        "failure",
        "RuntimeError: still bad",
        invocation="same",
        state_path=state_path,
        shared_root=tmp_path / "shared",
        host="chiap08",
    )
    assert result == "duplicate"
    assert AlertState.load(state_path).consecutive_failures == 1


def test_non_authority_counts_but_does_not_write_shared_state_or_mail(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    state_path = tmp_path / "alert.json"
    for index in range(FAILURE_THRESHOLD):
        result = observe(
            "failure",
            "TypeError: malformed",
            invocation=str(index),
            state_path=state_path,
            shared_root=tmp_path / "shared",
            host="chiap02",
            authority_host="chiap08",
            manager_factory=lambda _home: (_ for _ in ()).throw(AssertionError("shared write")),
            runner=_runner(calls),
        )
    assert result == "suppressed-non-authority"
    assert not calls


def test_authority_consumes_other_host_observation_once(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    manager = FakeITIL(tmp_path)
    shared = tmp_path / "shared"
    remote_state = tmp_path / "remote.json"
    for index in range(FAILURE_THRESHOLD):
        observe(
            "failure",
            "KeyError: remote CardStore line",
            invocation=f"remote-{index}",
            state_path=remote_state,
            shared_root=shared,
            host="chiap02",
            authority_host="chiap08",
            runner=_runner(calls),
        )
    result = observe(
        "success",
        "DISPATCH_EXPECTED|chiap08|count=0",
        invocation="authority-cycle",
        state_path=tmp_path / "authority.json",
        shared_root=shared,
        host="chiap08",
        authority_host="chiap08",
        manager_factory=lambda _home: manager,
        runner=_runner(calls),
    )
    assert result == "alerted"
    assert "chiap02: KeyError: remote CardStore line" in manager.created[0]["title"]
    assert len(manager.created) == 1


def test_five_hosts_produce_one_incident_and_two_deliveries(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    calls: list[list[str]] = []
    manager = FakeITIL(shared)
    for host in ("chiap01", "chiap02", "chiap03", "chiap04"):
        for index in range(FAILURE_THRESHOLD):
            observe(
                "failure",
                f"RuntimeError: {host} failed",
                invocation=f"{host}-{index}",
                state_path=tmp_path / host / "state.json",
                shared_root=shared,
                host=host,
                runner=_runner(calls),
            )
    results = []
    for index in range(FAILURE_THRESHOLD):
        results.append(
            observe(
                "failure",
                "RuntimeError: chiap08 failed",
                invocation=f"chiap08-{index}",
                state_path=tmp_path / "chiap08/state.json",
                shared_root=shared,
                host="chiap08",
                manager_factory=lambda _home: manager,
                runner=_runner(calls),
            )
        )
    assert "alerted" in results
    assert len(manager.created) == 1
    mail_calls = [call for call in calls if call[:2] == ["skmail", "send"]]
    assert len(mail_calls) == 2
    assert "chiap01,chiap02,chiap03,chiap04" in mail_calls[0][-1]


def test_missing_dispatch_marker_is_unknown_and_cannot_resolve(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    manager = FakeITIL(shared)
    calls: list[list[str]] = []
    state = tmp_path / "state.json"
    for index in range(FAILURE_THRESHOLD):
        observe(
            "failure",
            "RuntimeError: failed",
            invocation=f"failure-{index}",
            state_path=state,
            shared_root=shared,
            host="chiap08",
            manager_factory=lambda _home: manager,
            runner=_runner(calls),
        )
    result = observe(
        "success",
        "old launcher emitted no selection marker",
        invocation="unknown",
        state_path=state,
        shared_root=shared,
        host="chiap08",
        manager_factory=lambda _home: manager,
        runner=_runner(calls),
    )
    assert result == "observation-stale"
    assert not [item for item in manager.updated if item[1].get("new_status") == "resolved"]


def test_failed_mail_delivery_is_retried(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    manager = FakeITIL(shared)
    calls: list[list[str]] = []
    lumina_attempts = 0

    def runner(command, **_kwargs):
        nonlocal lumina_attempts
        calls.append(list(command))
        if command[:2] == ["skmail", "send"] and command[3] == "lumina":
            lumina_attempts += 1
            code = 1 if lumina_attempts == 1 else 0
            return subprocess.CompletedProcess(command, code, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    state = tmp_path / "state.json"
    for index in range(FAILURE_THRESHOLD):
        result = observe(
            "failure",
            "ValueError: alert me",
            invocation=str(index),
            state_path=state,
            shared_root=shared,
            host="chiap08",
            manager_factory=lambda _home: manager,
            runner=runner,
        )
    assert result == "delivery-pending"
    assert AlertState.load(state).delivered_to == ["jarvis"]
    result = observe(
        "failure",
        "ValueError: alert me",
        invocation="retry",
        state_path=state,
        shared_root=shared,
        host="chiap08",
        manager_factory=lambda _home: manager,
        runner=runner,
    )
    assert result == "alerted"
    assert lumina_attempts == 2
    assert len(manager.created) == 1


def test_stale_remote_observation_does_not_resolve_incident(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    manager = FakeITIL(shared)
    calls: list[list[str]] = []
    for index in range(FAILURE_THRESHOLD):
        observe(
            "failure",
            "KeyError: remote",
            invocation=f"remote-{index}",
            state_path=tmp_path / "remote.json",
            shared_root=shared,
            host="chiap02",
            clock=lambda: 1000,
            runner=_runner(calls),
        )
    authority = tmp_path / "authority.json"
    assert (
        observe(
            "success",
            "DISPATCH_EXPECTED|chiap08|count=0",
            invocation="open",
            state_path=authority,
            shared_root=shared,
            host="chiap08",
            clock=lambda: 1000,
            manager_factory=lambda _home: manager,
            runner=_runner(calls),
        )
        == "alerted"
    )
    result = observe(
        "success",
        "DISPATCH_EXPECTED|chiap08|count=0",
        invocation="later",
        state_path=authority,
        shared_root=shared,
        host="chiap08",
        clock=lambda: 3001,
        manager_factory=lambda _home: manager,
        runner=_runner(calls),
    )
    assert result == "observation-stale"
    assert not [item for item in manager.updated if item[1].get("new_status") == "resolved"]


def test_condition_change_reuses_incident(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    manager = FakeITIL(shared)
    calls: list[list[str]] = []
    state = tmp_path / "state.json"
    for index in range(FAILURE_THRESHOLD):
        observe(
            "failure",
            "TypeError: failed",
            invocation=f"failure-{index}",
            state_path=state,
            shared_root=shared,
            host="chiap08",
            manager_factory=lambda _home: manager,
            runner=_runner(calls),
        )
    stalled = "DISPATCH_EXPECTED|chiap08|count=1"
    for index in range(STALL_THRESHOLD):
        observe(
            "success",
            stalled,
            invocation=f"stall-{index}",
            state_path=state,
            shared_root=shared,
            host="chiap08",
            manager_factory=lambda _home: manager,
            runner=_runner(calls),
        )
    assert len(manager.created) == 1
    assert any("changed to silent-stall" in item[1].get("note", "") for item in manager.updated)


def test_traceback_frame_is_preserved() -> None:
    detail = extract_error(
        "prefix\nTraceback (most recent call last):\n"
        '  File "/opt/skfleet-rotate.py", line 100, in main\n'
        "    fold_event(event)\nAttributeError: malformed CardStore event"
    )
    assert 'File "/opt/skfleet-rotate.py", line 100' in detail
    assert "AttributeError: malformed CardStore event" in detail


def test_silent_stall_alerts_after_three_cycles_and_recovery_resolves(tmp_path: Path) -> None:
    manager = FakeITIL(tmp_path)
    calls: list[list[str]] = []
    state_path = tmp_path / "alert.json"
    shared = tmp_path / "shared"
    stalled = "POOL|chiap08|ready=4 sklegal=1 eng=2 biz=1\n" "DISPATCH_EXPECTED|chiap08|count=1"
    assert STALL_THRESHOLD == 3
    assert is_silent_stall(stalled)
    for index in range(STALL_THRESHOLD):
        result = observe(
            "success",
            stalled,
            invocation=f"stall-{index}",
            state_path=state_path,
            shared_root=shared,
            host="chiap08",
            manager_factory=lambda _home: manager,
            runner=_runner(calls),
        )
    assert result == "alerted"
    assert manager.created[0]["failure_class"] == "rotation-dispatch"

    result = observe(
        "success",
        "POOL|chiap08|ready=2 sklegal=0 eng=2 biz=0\n"
        "DISPATCH_EXPECTED|chiap08|count=1\nLAUNCHED|chiap08|codex-auto-a|a",
        invocation="healthy",
        state_path=state_path,
        shared_root=shared,
        host="chiap08",
        manager_factory=lambda _home: manager,
        runner=_runner(calls),
    )
    assert result == "recovered"
    assert manager.updated[0][0][0] == "inc-rotation"
    assert manager.updated[0][1]["new_status"] == "resolved"
    assert AlertState.load(state_path).alert_kind == ""


def test_error_fallback_and_cli_assets(tmp_path: Path, monkeypatch) -> None:
    assert extract_error("one line") == "one line"
    assert not is_silent_stall("POOL|chiap08|ready=4 sklegal=0 eng=4 biz=0")
    data = Path(__file__).parents[1] / "src/skcapstone/data/systemd"
    service = (data / "skfleet-rotate-alert.service").read_text(encoding="utf-8")
    drop_in = (data / "skfleet-rotate-alert.conf").read_text(encoding="utf-8")
    assert "MONITOR_INVOCATION_ID" in service
    assert "OnFailure=skfleet-rotate-alert.service" in drop_in
    assert "ExecStartPost=" in drop_in

    monkeypatch.setenv("SKCAPSTONE_SHARED_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv("SKFLEET_ALERT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKFLEET_ALERT_AUTHORITY", "not-this-host")
    monkeypatch.setattr(
        "skcapstone.fleet.cli.read_journal",
        lambda _unit, _invocation: (
            "POOL|test|ready=0 sklegal=0 eng=0 biz=0\nDISPATCH_EXPECTED|test|count=0"
        ),
    )
    result = CliRunner().invoke(fleet, ["rotate-alert", "success", "--invocation", "abc"])
    assert result.exit_code == 0
    assert result.output.strip() == "healthy"
    payload = json.loads((tmp_path / "state/skfleet-rotate-alert.json").read_text())
    assert payload["last_invocation"] == "abc"


def test_install_units_lands_drop_in_at_effective_path(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "src/skcapstone/data/systemd"
    service, drop_in = install_units(source, tmp_path / "systemd/user")
    assert service == tmp_path / "systemd/user/skfleet-rotate-alert.service"
    assert drop_in == tmp_path / "systemd/user/skfleet-rotate.service.d/alert.conf"
    assert service.read_bytes() == (source / "skfleet-rotate-alert.service").read_bytes()
    assert drop_in.read_bytes() == (source / "skfleet-rotate-alert.conf").read_bytes()
    assert service.stat().st_mode & 0o777 == 0o644
    assert drop_in.stat().st_mode & 0o777 == 0o644
