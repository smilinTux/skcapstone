"""Builder death requires local, complete managed execution proof."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_execution as execution


def _fixture(tmp_path, monkeypatch):
    """Supply an exact local unit and synthetic kernel membership tree."""
    monkeypatch.setattr(execution, "local_identity", lambda: ("node-test", "boot-test"))
    status = {
        "request_id": "request-one",
        "card_id": "1234abcd",
        "claim_revision": "claim-one",
        "execution_host": "node-test",
        "execution_boot_id": "boot-test",
    }
    status["execution_unit"] = execution.unit_name(
        status["card_id"], status["request_id"], status["claim_revision"]
    )
    group = tmp_path / status["execution_unit"]
    group.mkdir()
    (group / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    properties = {
        "Id": status["execution_unit"],
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "exited",
        "MainPID": "0",
        "ControlPID": "0",
        "ExecMainPID": "123",
        "ExecMainCode": "1",
        "ExecMainStatus": "1",
        "ControlGroup": "/" + group.name,
        "KillMode": "control-group",
        "Restart": "no",
        "RemainAfterExit": "yes",
    }

    def runner(argv):
        assert argv[:3] == ["systemctl", "--user", "show"]
        return SimpleNamespace(
            returncode=0, stdout="\n".join(f"{k}={v}" for k, v in properties.items())
        )

    return status, group, properties, runner


def test_confirmed_empty_complete_local_unit_is_dead(tmp_path, monkeypatch):
    status, _, _, runner = _fixture(tmp_path, monkeypatch)
    result = execution.observe(status, runner=runner, cgroup_root=tmp_path)
    assert result.alive is False and result.exit_code == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_host", "other-node"),
        ("execution_boot_id", "old-boot"),
        ("execution_unit", "other.service"),
    ],
)
def test_foreign_or_incomplete_identity_never_establishes_death(
    tmp_path, monkeypatch, field, value
):
    status, _, _, runner = _fixture(tmp_path, monkeypatch)
    status[field] = value
    assert execution.observe(status, runner=runner, cgroup_root=tmp_path).alive is None
    assert execution.observe({"pid": 123, "pid_start_ticks": "456"}).alive is None


def test_leader_exit_with_surviving_children_is_alive(tmp_path, monkeypatch):
    status, group, _, runner = _fixture(tmp_path, monkeypatch)
    (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    assert execution.observe(status, runner=runner, cgroup_root=tmp_path).alive is True


def test_denied_kernel_read_is_unknown(tmp_path, monkeypatch):
    status, _, _, runner = _fixture(tmp_path, monkeypatch)

    def denied(*args, **kwargs):
        raise PermissionError("synthetic denied kernel state")

    monkeypatch.setattr(type(tmp_path), "read_text", denied)
    assert execution.observe(status, runner=runner, cgroup_root=tmp_path).alive is None


def test_unknown_unit_or_missing_cgroup_never_means_dead(tmp_path, monkeypatch):
    status, group, properties, runner = _fixture(tmp_path, monkeypatch)
    properties["LoadState"] = "not-found"
    assert execution.observe(status, runner=runner, cgroup_root=tmp_path).alive is None
    properties["LoadState"] = "loaded"
    (group / "cgroup.events").unlink()
    assert execution.observe(status, runner=runner, cgroup_root=tmp_path).alive is None


def test_launch_uses_persistent_dedicated_unit_without_shell(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        execution.subprocess,
        "run",
        lambda argv, **kw: (
            calls.append((argv, kw)) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    unit = execution.unit_name("1234abcd", "request-one", "claim-one")
    execution.launch(["/test/pi", "--name", "worker"], tmp_path, unit)
    argv, kwargs = calls[0]
    assert argv[:2] == ["systemd-run", "--user"]
    assert "--property=KillMode=control-group" in argv
    assert "--property=RemainAfterExit=yes" in argv
    assert "--property=Restart=no" in argv
    assert "--collect" not in argv and "--wait" not in argv
    assert argv[-3:] == ["/test/pi", "--name", "worker"]
    assert kwargs.get("shell", False) is False
