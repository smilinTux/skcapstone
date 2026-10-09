"""Readiness grades the fixed native-worker context without card actuation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/fleet"))
import skfleet_readiness as readiness


@pytest.mark.parametrize("failures", [[], ["bwrap Python refused or invalid"]])
def test_fixed_probe_uses_bounded_sibling_service(monkeypatch, failures):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            int(bool(failures)),
            json.dumps({"schema": "skfleet.sandbox-readiness/v1", "failures": failures}),
            "",
        )

    monkeypatch.setattr(readiness.subprocess, "run", run)
    monkeypatch.setenv("BASH_ENV", "/untrusted/startup")
    error = readiness.qualification_sandbox_error(sys.executable)
    assert (error is None) == (not failures)
    command, options = calls[0]
    assert command[:2] == ["/usr/bin/systemd-run", "--user"]
    assert "--wait" in command and "--collect" in command and "--pipe" in command
    assert "--property=MemoryMax=256M" in command
    assert "--property=RuntimeMaxSec=30" in command
    assert "--property=CPUQuota=100%" in command
    assert "--property=TasksMax=64" in command
    assert "--property=UnsetEnvironment=BASH_ENV" in command
    assert command[command.index("--") + 1 :] == [
        sys.executable,
        "-I",
        "-m",
        "skcapstone.fleet.production_sandbox_probe",
    ]
    assert "BASH_ENV" not in options["env"] and options["timeout"] == 40


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[]",
        "{}",
        '{"failures": []}',
        '{"schema":"skfleet.sandbox-readiness/v1","failures":""}',
    ],
)
def test_incomplete_or_invalid_service_result_is_not_ready(monkeypatch, raw):
    monkeypatch.setattr(
        readiness.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, raw, ""),
    )
    assert readiness.qualification_sandbox_error(sys.executable)


def test_diagnostic_entrypoint_runs_existing_full_gate(monkeypatch, capsys):
    from skcapstone.fleet import production_sandbox_probe as probe
    from skcapstone.fleet import sandbox_tools

    calls = []
    monkeypatch.setattr(
        sandbox_tools, "readiness", lambda python: calls.append(python) or ["enforcement missing"]
    )
    assert probe.main() == 1
    # The sandbox binds only the clean qualification prefix, so the probe
    # must exercise that interpreter, not the caller's own.
    assert calls == [str(probe.PREFIX / "bin/python")]
    assert json.loads(capsys.readouterr().out)["failures"] == ["enforcement missing"]


def test_existing_readiness_filesystem_hardening_is_retained():
    root = Path(__file__).resolve().parents[2]
    for path in (
        root / "systemd/skfleet-readiness.service",
        root / "src/skcapstone/data/systemd/skfleet-readiness.service",
    ):
        text = path.read_text()
        for property in (
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
        ):
            assert property in text
