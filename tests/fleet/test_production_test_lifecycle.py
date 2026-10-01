"""Retained native service lifecycle and qualified dependency drift regressions."""

from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_tests as native
from tests.fleet.test_production_tests import receipt_fixture
from tests.fleet.test_production_tests import setup as setup


def test_runtime_drift_invalidates_operator_plan(setup, monkeypatch):
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "e" * 64)
    with pytest.raises(native.TestEvidenceError, match="stale"):
        native.load_plan(setup.home, setup.binding)


@pytest.mark.parametrize(
    "group,tasks,main,expected",
    [
        ("", "[not set]", "0", True),
        ("/user.slice/empty", "0", "0", True),
        ("/user.slice/live", "[not set]", "0", False),
        ("", "0", "0", False),
        ("", "[not set]", "42", False),
        (None, None, "0", False),
    ],
)
def test_only_exact_empty_cgroup_states_are_terminal(group, tasks, main, expected):
    assert (
        native.terminal_cgroup({"ControlGroup": group, "TasksCurrent": tasks, "MainPID": main})
        is expected
    )


def test_stop_requires_same_invocation_and_reaps_wait_child(setup, monkeypatch):
    _, terminal = receipt_fixture(setup, monkeypatch)
    launch = native.read_json(setup.directory / "launch.json")
    calls, waits = [], []
    native._SERVICE_PROCESSES[launch["unit"]] = SimpleNamespace(wait=lambda **kw: waits.append(kw))

    def response(invocation):
        return SimpleNamespace(
            stdout="LoadState=loaded\nActiveState=active\nSubState=exited\n"
            "InvocationID=" + invocation + "\nMainPID=0\nControlGroup=\nTasksCurrent=[not set]\n"
        )

    monkeypatch.setattr(native.subprocess, "run", lambda *a, **kw: response("e" * 32))
    with pytest.raises(native.TestEvidenceError, match="refusing stop"):
        native.stop_retained(setup.directory, launch)
    assert not waits

    def run(argv, **kwargs):
        calls.append(argv)
        return response(terminal["InvocationID"])

    monkeypatch.setattr(native.subprocess, "run", run)
    native.stop_retained(setup.directory, launch)
    assert calls[-1] == ["/usr/bin/systemctl", "--user", "stop", launch["unit"]]
    assert waits == [{"timeout": 5}]


def test_lost_stop_ack_never_replays_stop_against_reused_unit(setup, monkeypatch):
    receipt_fixture(setup, monkeypatch)
    launch = native.read_json(setup.directory / "launch.json")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="LoadState=not-found\n")

    monkeypatch.setattr(native.subprocess, "run", run)
    native.stop_retained(setup.directory, launch)
    assert len(calls) == 1 and "show" in calls[0]


def test_failed_unit_is_retained_as_failure_without_reserving_forever(setup, monkeypatch):
    receipt, terminal = receipt_fixture(setup, monkeypatch)
    (setup.directory / "terminal.json").unlink()
    launch = native.read_json(setup.directory / "launch.json")
    terminal.update(ActiveState="failed", SubState="failed", ExecMainStatus="1")
    response = "\n".join(key + "=" + str(value) for key, value in terminal.items())
    monkeypatch.setattr(
        native.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=response)
    )
    assert native.observe_terminal(setup.directory, launch)
    native.stop_retained(setup.directory, launch)
    with pytest.raises(native.TestEvidenceError):
        native.validate_test_receipt(setup.home, setup.binding, setup.workspace)


def test_runtime_fingerprint_detects_changed_metadata_and_module(tmp_path, monkeypatch):
    monkeypatch.setattr(plan, "PREFIX", tmp_path)
    site = tmp_path / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/ruff").write_bytes(b"ruff")
    for name in (
        "pytest",
        "_pytest",
        "ruff",
        "skcoord",
        "skcapstone",
        "pydantic",
        "pydantic_core",
        "pluggy",
        "yaml",
        "rich",
        "click",
    ):
        (site / name).mkdir()
        (site / name / "__init__.py").write_text("initial = True\n")
    metadata = site / "pytest-1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Version: 1\n")
    first = plan.runtime_fingerprint()
    assert first == plan.runtime_fingerprint()
    (metadata / "METADATA").write_text("Version: 2\n")
    second = plan.runtime_fingerprint()
    assert second != first
    (site / "skcoord/__init__.py").write_text("changed = True\n")
    assert plan.runtime_fingerprint() != second


@pytest.mark.parametrize("invocation_matches", [True, False])
def test_stopped_loaded_unit_replay_requires_same_invocation(
    setup, monkeypatch, invocation_matches
):
    _, terminal = receipt_fixture(setup, monkeypatch)
    invocation = terminal["InvocationID"] if invocation_matches else "e" * 32
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            stdout="LoadState=loaded\nActiveState=inactive\nSubState=dead\n"
            "InvocationID=" + invocation + "\nMainPID=0\nControlGroup=\nTasksCurrent=[not set]\n"
        )

    monkeypatch.setattr(native.subprocess, "run", run)
    if invocation_matches:
        result = native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy)
        assert result["counts"]["total"] == 226
    else:
        with pytest.raises(native.TestEvidenceError, match="refusing stop"):
            native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy)
    assert len(calls) == 1 and "show" in calls[0]
