from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/fleet/skfleet-working.py"


def load_module(monkeypatch, **env):
    for key in ("SKFLEET_HOSTS", "SKFLEET_REMOTE_PYTHON"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("skfleet_working_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_hosts_include_live_builder(monkeypatch):
    module = load_module(monkeypatch)
    assert "ziowk01" in module.HOSTS
    assert module.HOSTS.count("ziowk01") == 1
    assert module.HOSTS[:5] == ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def test_explicit_hosts_remain_authoritative(monkeypatch):
    module = load_module(monkeypatch, SKFLEET_HOSTS="ziowk01 chiap04")
    assert module.HOSTS == ("ziowk01", "chiap04")


def test_collect_selects_runtime_with_skcoord_before_system_python(monkeypatch):
    module = load_module(monkeypatch, SKFLEET_REMOTE_PYTHON="$HOME/.skenv/bin/python3")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return type("Result", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.collect("ziowk01")
    command = captured["command"]
    assert command[command.index("sh") + 1] == "-lc"
    script = command[-1]
    assert '"$HOME/.skenv/bin/python3" python3' in script
    assert "import skcoord" in script
    assert 'exec "$py" -c' in script


def test_runtime_python_can_be_overridden(monkeypatch):
    module = load_module(monkeypatch, SKFLEET_REMOTE_PYTHON="/opt/skcapstone/bin/python3")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return type("Result", (), {"returncode": 0, "stdout": ""})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.collect("chiap04")
    assert "for py in /opt/skcapstone/bin/python3 python3" in captured["command"][-1]
