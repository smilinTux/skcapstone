"""D2 test-only adapter: keep real bwrap/Git, never operate the service manager."""

import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_live_service_mutation(monkeypatch):
    run = subprocess.run

    def isolated(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and command:
            executable = Path(str(command[0])).name
            if executable == "systemd-run":
                if "/usr/bin/bwrap" not in command:
                    raise AssertionError("D2 tests cannot launch services")
                command = command[command.index("/usr/bin/bwrap") :]
            elif executable == "systemctl":
                if "stop" in command and any(
                    str(x).startswith("skfleet-inspect-") for x in command
                ):
                    return subprocess.CompletedProcess(command, 0)
                raise AssertionError("D2 tests require a fixture for service observations")
            elif executable == "ssh":
                raise AssertionError("D2 tests cannot access remote services")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", isolated)
