"""Canonical observer exercises the real native parser without dispatching."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/fleet"))
import skfleet_readiness as readiness


@pytest.fixture
def production(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    host = socket.gethostname().split(".")[0].lower()
    policy = tmp_path / "production.json"
    policy.write_text(
        json.dumps(
            {
                "schema": "skfleet.production/v1",
                "authority_host": host,
                "capacity_authority": "skgateway",
                "gateway_url": "http://gateway.test:18790",
                "lanes": {
                    **{
                        lane: {"enabled": True, "provider": "skgateway"}
                        for lane in ("codex", "glm", "deepseek", "qwen")
                    },
                    "kimi": {"enabled": False},
                },
            }
        )
    )
    environment = {"SKFLEET_PRODUCTION_POLICY": str(policy), "SKFLEET_AUTHORITY_HOST": host}
    return policy, environment


def test_native_policy_parser_and_real_authority_are_required(production):
    policy, environment = production
    assert readiness.production_environment_error(environment, sys.executable) is None
    assert readiness.production_environment_error(
        {**environment, "SKFLEET_AUTHORITY_HOST": "wrong"}, sys.executable
    )
    value = json.loads(policy.read_text())
    value["lanes"]["kimi"]["enabled"] = True
    policy.write_text(json.dumps(value))
    assert readiness.production_environment_error(environment, sys.executable)


def test_missing_and_nonregular_policy_fail_closed(production, tmp_path):
    policy, environment = production
    policy.unlink()
    assert readiness.production_environment_error(environment, sys.executable)
    policy.symlink_to(tmp_path)
    assert readiness.production_environment_error(environment, sys.executable)


@pytest.mark.parametrize(
    "broken_import,extra_required", [(False, False), (True, False), (False, True)]
)
def test_production_observer_skips_only_legacy_caps_preserves_imports_and_other_env(
    production, tmp_path, monkeypatch, capsys, broken_import, extra_required
):
    _, environment = production
    source = tmp_path / "dispatcher.py"
    source.write_text(
        "import " + ("nonexistent_native_readiness_module" if broken_import else "json") + "\n"
        'TARGET=_required_lane_target("SKFLEET_TARGET")\n'
        'raise SystemExit("SKFLEET_GATEWAY_URL is required")\n'
        + ('raise SystemExit("REAL_OTHER_REQUIREMENT is required")\n' if extra_required else "")
    )
    units = tmp_path / "units"
    units.mkdir()
    (units / "skfleet-seat-cycle.service").write_text("ExecStart=/usr/bin/python3 -m json\n")
    observed = []

    def scope(unit):
        observed.append(unit)
        return True, None, "skfleet-seat-cycle.timer"

    monkeypatch.setattr(readiness, "unit_in_scope", scope)
    monkeypatch.setattr(
        readiness, "systemd_effective_environment", lambda unit: (environment, None)
    )
    result = readiness._run(source, units, sys.executable, None, "skfleet-seat-cycle.service")
    assert result == (1 if broken_import or extra_required else 0)
    output = capsys.readouterr().out
    assert "OK production policy" in output
    assert "SKFLEET_TARGET is missing" not in output
    assert "SKFLEET_GATEWAY_URL is missing" not in output
    assert observed == ["skfleet-seat-cycle.service"]
    if broken_import:
        assert "FAIL dispatcher import" in output
    if extra_required:
        assert "REAL_OTHER_REQUIREMENT is missing" in output


def test_unavailable_native_validation_fails_closed(production, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("native-parser", 15)

    monkeypatch.setattr(readiness.subprocess, "run", fail)
    assert readiness.production_environment_error(production[1], sys.executable)


def test_canonical_scheduler_cannot_fall_back_to_legacy_env(tmp_path, monkeypatch, capsys):
    source = tmp_path / "dispatcher.py"
    source.write_text("import json\n")
    units = tmp_path / "units"
    units.mkdir()
    (units / "seat.service").write_text("ExecStart=/usr/bin/python3 -m json\n")
    monkeypatch.setattr(
        readiness, "unit_in_scope", lambda unit: (True, None, "skfleet-seat-cycle.timer")
    )
    monkeypatch.setattr(readiness, "systemd_effective_environment", lambda unit: ({}, None))
    assert readiness._run(source, units, sys.executable, None, "skfleet-seat-cycle.service") == 1
    assert "FAIL production policy" in capsys.readouterr().out


def test_canonical_readiness_template_uses_scheduler_scope():
    text = (
        Path(__file__).resolve().parents[2] / "systemd/production/skfleet-readiness.service"
    ).read_text()
    assert "--rotate-script %h/.skenv/bin/skfleet-rotate.py" in text
    assert "--env-from-systemd skfleet-seat-cycle.service" in text
    assert "niobe-live" not in text
