"""Retirement reuses exact terminal proof before writing source custody."""

import json
import os
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_retire as retire
from skcapstone.fleet import builder_terminal as terminal
from tests.fleet.test_builder_retire import case as case, run, write


@pytest.fixture
def terminal_case(case, monkeypatch):
    """Supply only system observations, retaining both real proof functions."""
    c = case
    c.request["production"] = {"host": "worker"}
    c.status.update(
        production=c.request["production"],
        invocation="d" * 32,
        pid=2147483647,
        pid_start_ticks="1",
    )
    c.status["unit"] = terminal.production_builder.unit_name(c.status, 1)
    c.values = dict(
        LoadState="not-found",
        ActiveState="inactive",
        SubState="dead",
        MainPID="0",
        InvocationID="",
        ExecMainStatus="0",
    )
    boot = terminal.boot_id()
    shared = dict(
        _BOOT_ID=boot,
        _UID=str(os.getuid()),
        _PID="99",
        _EXE="/usr/lib/systemd/systemd",
        _SYSTEMD_CGROUP=f"/user.slice/user-{os.getuid()}.slice/"
        f"user@{os.getuid()}.service/init.scope",
        USER_UNIT=c.status["unit"],
        USER_INVOCATION_ID=c.status["invocation"],
    )
    c.rows = [
        {
            **shared,
            "MESSAGE_ID": terminal.START_MESSAGE,
            "JOB_TYPE": "start",
            "JOB_RESULT": "done",
            "__CURSOR": "start",
            "__MONOTONIC_TIMESTAMP": "2000000",
        },
        {
            **shared,
            "MESSAGE_ID": terminal.RESOURCE_MESSAGE,
            "CODE_FILE": "src/core/unit.c",
            "CODE_FUNC": "unit_log_resources",
            "__CURSOR": "terminal",
            "__MONOTONIC_TIMESTAMP": "3000000",
        },
    ]
    c.workspace = c.paths.root / "workspaces" / c.status["owner"]
    c.workspace.mkdir(parents=True)
    write(c.workspace / "staged", b"preserve me")
    monkeypatch.setattr(retire.source_bundle, "_inspect", lambda *a: {"head": "b" * 40})

    def command(argv, **kwargs):
        if argv[0] == "systemctl":
            raw = "\n".join(k + "=" + v for k, v in c.values.items())
        elif argv[0] == "journalctl":
            raw = "\n".join(json.dumps(row) for row in c.rows)
        else:
            assert argv[0] == "dpkg-query"
            raw = terminal.QUALIFICATION["version"]
        return SimpleNamespace(returncode=0, stdout=raw)

    monkeypatch.setattr(terminal.subprocess, "run", command)
    c.kwargs["probe"] = lambda host, payload: retire.node_check(
        payload, paths=c.paths, home=c.home, locked=True
    )
    return c


def pin(c):
    """Pin exact synthetic request and status bytes for the controller."""
    write(c.req, json.dumps(c.request).encode())
    write(c.sts, json.dumps(c.status).encode())
    c.kwargs.update(
        request_sha256=retire.sha(c.req.read_bytes()),
        status_sha256=retire.sha(c.sts.read_bytes()),
    )


@pytest.mark.parametrize("loaded", [False, True])
def test_retirement_loaded_and_collected_exact_proof(terminal_case, loaded):
    c = terminal_case
    if loaded:
        c.values.update(LoadState="loaded", InvocationID=c.status["invocation"])
    pin(c)
    original = c.sts.read_bytes(), retire.inventory(c.workspace)
    assert run(c)["state"] == "qualified-check-only"
    assert not (c.home / "evidence").exists()
    assert run(c, apply=True)["state"] == "retired"
    assert (c.sts.read_bytes(), retire.inventory(c.workspace)) == original


@pytest.mark.parametrize("change", ["live", "reused", "invocation", "later"])
def test_failed_terminal_proof_never_mutates_custody(terminal_case, change):
    c = terminal_case
    if change == "live":
        c.values.update(
            LoadState="loaded",
            ActiveState="active",
            MainPID="99",
            InvocationID=c.status["invocation"],
        )
    elif change == "reused":
        c.status["pid"] = os.getpid()
    elif change == "invocation":
        c.values.update(LoadState="loaded", InvocationID="e" * 32)
    else:
        c.rows[-1]["USER_INVOCATION_ID"] = "e" * 32
    pin(c)
    original = c.req.read_bytes(), c.sts.read_bytes(), retire.inventory(c.workspace)
    with pytest.raises(ValueError):
        run(c, apply=True)
    assert (
        c.req.read_bytes(),
        c.sts.read_bytes(),
        retire.inventory(c.workspace),
    ) == original
    assert not (c.home / "evidence").exists()
