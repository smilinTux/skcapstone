"""sk-alert resolution honours $SK_ALERT_BIN, and the suite never alerts for real."""

from __future__ import annotations

import subprocess

from skcapstone import sk_alert_bin as mod
from skcapstone.fleet import alerts


def test_suite_default_points_alerts_at_bin_true():
    assert mod.sk_alert_bin() == "/bin/true"


def test_override_wins(monkeypatch):
    monkeypatch.setenv("SK_ALERT_BIN", "/opt/x/sk-alert")
    assert mod.sk_alert_bin() == "/opt/x/sk-alert"


def test_path_then_venv_fallback(monkeypatch):
    monkeypatch.delenv("SK_ALERT_BIN", raising=False)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/sk-alert")
    assert mod.sk_alert_bin() == "/usr/bin/sk-alert"
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod.sk_alert_bin().endswith("/.skenv/bin/sk-alert")


def test_fleet_alert_uses_override(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert alerts.send_alert("fleet: test", level="error") is True
    assert seen["cmd"][0] == "/bin/true"


def test_operator_seat_sender_uses_override(monkeypatch):
    from skcapstone.operator_seat import notify

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert notify.default_sender("hi") is True
    assert seen["cmd"][0] == "/bin/true"
