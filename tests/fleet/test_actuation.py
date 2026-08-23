"""Tests for the systemd --user actuation verbs (all runners faked)."""

from __future__ import annotations

from subprocess import CompletedProcess

from skcapstone.fleet import actuation


class FakeRunner:
    """Records every command; replies from a canned map."""

    def __init__(self, replies: dict[str, tuple[int, str]]) -> None:
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)
        code, out = self.replies.get(" ".join(cmd), (0, ""))
        return CompletedProcess(cmd, code, stdout=out, stderr="")


SHOW = (
    "systemctl --user show skgateway.service "
    "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp"
)


def test_state_active() -> None:
    runner = FakeRunner(
        {
            SHOW: (
                0,
                "LoadState=loaded\nActiveState=active\n"
                "MainPID=4242\n"
                "ActiveEnterTimestamp=Mon 2026-07-27 09:00:00 UTC\n",
            )
        }
    )
    st = actuation.systemd_state("skgateway.service", runner=runner)
    assert st.state == "active" and st.pid == 4242
    assert st.since == "Mon 2026-07-27 09:00:00 UTC"


def test_state_failed_and_missing_and_unknown() -> None:
    runner = FakeRunner(
        {SHOW: (0, "LoadState=loaded\nActiveState=failed\nMainPID=0\n" "ActiveEnterTimestamp=\n")}
    )
    assert actuation.systemd_state("skgateway.service", runner=runner).state == "failed"
    runner = FakeRunner(
        {
            SHOW: (
                0,
                "LoadState=not-found\nActiveState=inactive\n" "MainPID=0\nActiveEnterTimestamp=\n",
            )
        }
    )
    assert actuation.systemd_state("skgateway.service", runner=runner).state == "missing"
    runner = FakeRunner({SHOW: (1, "")})
    st = actuation.systemd_state("skgateway.service", runner=runner)
    assert st.state == "unknown" and st.pid is None


def test_start_restart_and_logs() -> None:
    runner = FakeRunner(
        {
            "systemctl --user start u.service": (0, ""),
            "systemctl --user restart u.service": (1, ""),
            "journalctl --user -u u.service -n 30 --no-pager": (0, "boom line\n"),
        }
    )
    assert actuation.systemd_start("u.service", runner=runner) is True
    assert actuation.systemd_restart("u.service", runner=runner) is False
    assert actuation.systemd_logs("u.service", runner=runner) == "boom line"
    assert runner.calls[0] == ["systemctl", "--user", "start", "u.service"]


def test_runner_exception_degrades_to_unknown() -> None:
    def boom(cmd: list[str]):
        raise OSError("no systemd here")

    st = actuation.systemd_state("u.service", runner=boom)
    assert st.state == "unknown"
    assert actuation.systemd_start("u.service", runner=boom) is False
    assert actuation.systemd_logs("u.service", runner=boom) == ""


def test_default_runner_shape(monkeypatch) -> None:
    import subprocess

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    actuation.default_runner(["systemctl", "--user", "--version"])
    assert seen["cmd"] == ["systemctl", "--user", "--version"]
    assert seen["kw"]["capture_output"] is True and seen["kw"]["timeout"] == 30
