"""Tests for bounded crash-loop backoff (10s doubling to 300s, then stop)."""

from __future__ import annotations

import pytest

from skcapstone.fleet import alerts, backoff


@pytest.fixture(autouse=True)
def _fresh():
    backoff.reset_trackers()
    yield
    backoff.reset_trackers()


def test_delay_schedule_doubles_and_caps() -> None:
    assert backoff.next_delay(0) == 0.0  # first heal is immediate
    assert backoff.next_delay(1) == 10.0
    assert backoff.next_delay(2) == 20.0
    assert backoff.next_delay(3) == 40.0
    assert backoff.next_delay(6) == 300.0  # capped at 5 minutes
    assert backoff.next_delay(50) == 300.0


def test_allowed_respects_delay() -> None:
    track = backoff.tracker("node-41", "skgateway")
    assert backoff.allowed(track, now=1000.0) is True
    backoff.record_attempt(track, now=1000.0)
    assert backoff.allowed(track, now=1005.0) is False  # 10s not elapsed
    assert backoff.allowed(track, now=1010.0) is True
    backoff.record_attempt(track, now=1010.0)
    assert backoff.allowed(track, now=1025.0) is False  # now needs 20s
    assert backoff.allowed(track, now=1030.0) is True


def test_bounded_attempts_then_crash_looping() -> None:
    track = backoff.tracker("node-41", "skgateway")
    now = 1000.0
    for _ in range(backoff.CRASH_LOOP_AFTER):
        assert backoff.is_crash_looping(track) is False
        now += backoff.next_delay(track["attempts"])
        assert backoff.allowed(track, now) is True
        backoff.record_attempt(track, now)
    assert backoff.is_crash_looping(track) is True  # bounded: healing stops


def test_healthy_reset_clears_the_episode() -> None:
    track = backoff.tracker("node-41", "skgateway")
    for i in range(backoff.CRASH_LOOP_AFTER):
        backoff.record_attempt(track, now=1000.0 + i * 400.0)
    assert backoff.is_crash_looping(track) is True
    last = track["last_attempt"]
    backoff.record_healthy(track, now=last + 60.0)  # too soon: no reset
    assert backoff.is_crash_looping(track) is True
    backoff.record_healthy(track, now=last + backoff.HEALTHY_RESET_S + 1.0)
    assert track["attempts"] == 0
    assert backoff.is_crash_looping(track) is False


def test_trackers_are_per_service() -> None:
    a = backoff.tracker("node-41", "svc-a")
    backoff.record_attempt(a, now=1000.0)
    assert backoff.tracker("node-41", "svc-b")["attempts"] == 0
    assert backoff.tracker("node-41", "svc-a") is a


def test_send_alert_never_raises(monkeypatch) -> None:
    import subprocess

    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert alerts.send_alert("fleet: skgateway CrashLooping", level="error") is True
    assert calls["cmd"][-1] == "fleet: skgateway CrashLooping"
    assert "-l" in calls["cmd"] and "error" in calls["cmd"]

    def boom(cmd, **kw):
        raise OSError("no sk-alert")

    monkeypatch.setattr(subprocess, "run", boom)
    assert alerts.send_alert("still fine") is False  # best-effort, no raise
