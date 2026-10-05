"""Sandbox readiness and main-only AppArmor rollout, without host mutation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skcapstone.fleet import rollout_artifacts, staged_rollout

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/fleet"))
import skfleet_readiness as readiness


@pytest.mark.parametrize("exit_code", [0, 1])
def test_probe_observes_actual_native_sandbox_exit(monkeypatch, exit_code):
    """Namespace refusal cannot be mistaken for import readiness."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            exit_code,
            json.dumps(
                {
                    "schema": "skfleet.sandbox-readiness/v1",
                    "failures": ["net_admin denied"] if exit_code else [],
                }
            ),
            "",
        )

    monkeypatch.setattr(readiness.subprocess, "run", run)
    monkeypatch.setenv("BASH_ENV", "/untrusted/startup")
    error = readiness.qualification_sandbox_error(sys.executable)
    assert (error is None) == (exit_code == 0)
    command = calls[0][0]
    assert command[:2] == ["/usr/bin/systemd-run", "--user"]
    assert command[command.index("--") + 1 :] == [
        sys.executable,
        "-I",
        "-m",
        "skcapstone.fleet.production_sandbox_probe",
    ]
    assert "BASH_ENV" not in calls[0][1]["env"]
    assert calls[0][1]["timeout"] == 40
    if error:
        assert "net_admin denied" in error


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("probe", 20)])
def test_missing_binary_or_timeout_is_not_ready(monkeypatch, failure):
    """Unavailable execution is a failed gate, never a successful empty check."""

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(readiness.subprocess, "run", run)
    assert readiness.qualification_sandbox_error(sys.executable)


def test_seat_host_checks_sandbox_even_with_inactive_authority_timer(tmp_path, monkeypatch):
    """The false green happened on chiap01 while authority ran on chiap08."""
    dispatcher = tmp_path / "dispatcher.py"
    dispatcher.write_text("pass\n")
    units = tmp_path / "units"
    units.mkdir()
    (units / "sknoded.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
    monkeypatch.setattr(readiness, "unit_in_scope", lambda _: (False, None, "seat.timer"))
    monkeypatch.setattr(readiness, "qualification_sandbox_error", lambda _: "net_admin denied")
    verdict = tmp_path / "verdict.json"
    assert (
        readiness._run(
            dispatcher, units, sys.executable, None, "skfleet-seat-cycle.service", verdict
        )
        == 1
    )
    assert "FAIL qualification sandbox: net_admin denied" in verdict.read_text()


def test_rollout_installs_only_shipped_profile_and_preserves_preimage(tmp_path, monkeypatch):
    """Root installation remains in the rollout, without changing global sysctls."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    policy = home / ".skcapstone/fleet/production.json"
    policy.parent.mkdir(parents=True)
    policy.write_text("{}")
    source = repo / "systemd/apparmor/skfleet-bwrap"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new managed profile\n")
    target = tmp_path / "etc/apparmor.d/skfleet-bwrap"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old managed profile\n")
    monkeypatch.setattr(rollout_artifacts, "SANDBOX_PROFILE_TARGET", target)
    calls = []
    monkeypatch.setattr(rollout_artifacts.subprocess, "run", lambda args, **kw: calls.append(args))
    rollout_artifacts.install_sandbox_profile(repo, home)
    assert calls == [
        ["sudo", "-n", "/usr/bin/install", "-m", "0644", str(source), str(target)],
        ["sudo", "-n", "/usr/sbin/apparmor_parser", "-r", str(target)],
    ]
    backups = list((home / ".skcapstone/fleet/rollout-apparmor-preimages").glob("*/skfleet-bwrap"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"old managed profile\n"
    names = [name for name, _ in staged_rollout._DEPLOY_STEPS]
    assert (
        names.index("pip_install")
        < names.index("install_sandbox_profile")
        < names.index("converge")
    )


def test_missing_profile_does_not_invoke_privileged_commands(tmp_path, monkeypatch):
    """Never synthesize policy bytes if a checkout is incomplete."""
    monkeypatch.setattr(
        rollout_artifacts.subprocess,
        "run",
        lambda *args, **kw: pytest.fail("privileged command attempted"),
    )
    home = tmp_path / "home"
    policy = home / ".skcapstone/fleet/production.json"
    policy.parent.mkdir(parents=True)
    policy.write_text("{}")
    with pytest.raises(FileNotFoundError):
        rollout_artifacts.install_sandbox_profile(tmp_path, home)


def test_nonproduction_rollout_does_not_change_apparmor(tmp_path, monkeypatch):
    """Legacy fleet hosts are not implicitly granted production sandbox policy."""
    monkeypatch.setattr(
        rollout_artifacts.subprocess,
        "run",
        lambda *args, **kw: pytest.fail("privileged command attempted"),
    )
    rollout_artifacts.install_sandbox_profile(tmp_path, tmp_path / "home")
