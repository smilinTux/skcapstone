"""Native test evidence is source-bound, private, complete and never replayed."""

import copy
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from skcapstone.fleet import production_resources as resources
from skcapstone.fleet import production_test_worker as worker
from skcapstone.fleet import production_tests as native


def private_bytes(path, raw):
    """Create private fixture evidence with exact bytes."""
    path.write_bytes(raw)
    path.chmod(0o600)


def junit():
    """Build the minimum qualified coverage with independently counted testcases."""
    root = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(
        root, "testsuite", tests="226", failures="0", errors="0", skipped="0"
    )
    for index, filename in enumerate(native.TEST_FILES):
        count = 19 if index < 10 else 18
        for number in range(count):
            ElementTree.SubElement(
                suite,
                "testcase",
                classname="tests." + Path(filename).stem,
                name="test_" + str(number),
            )
    return ElementTree.tostring(root)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Use a real clean Git source and private immutable plan fixture."""
    workspace = tmp_path / "source"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )

    def revision(ref):
        return subprocess.check_output(
            ["git", "-C", str(workspace), "rev-parse", ref], text=True
        ).strip()

    binding = {
        "source_card": "89508f83",
        "source_owner": "producer",
        "source_claim_revision": "claim",
        "source_head": revision("HEAD"),
        "source_tree": revision("HEAD^{tree}"),
        "source_revision": "revision",
        "criteria_sha256": "c" * 64,
    }
    home = tmp_path / "home"
    (home / "fleet").mkdir(parents=True)
    host = socket.gethostname().split(".")[0].lower()
    policy = {
        "authority_host": host,
        "node_quotas": {
            host: {
                "cpu_quota_percent": 200,
                "memory_max_bytes": 3 * 1024**3,
                "tasks_max": 256,
                "runtime_max_seconds": 3600,
            }
        },
    }
    plan_path = native.seal_plan(home, binding, workspace, policy, "operator", "a" * 64)
    plan, _, digest = native.load_plan(home, binding)
    monkeypatch.setattr(native, "active_resource_units", lambda *args: [])
    monkeypatch.setattr(native, "local_worker_admission", lambda *args: (True, "fixture"))
    return SimpleNamespace(
        home=home,
        binding=binding,
        workspace=workspace,
        policy=policy,
        plan=plan,
        plan_path=plan_path,
        directory=native.run_directory(home, digest),
        digest=digest,
    )


def launch_fixture(setup, monkeypatch):
    """Fixture only: capture a requested launch without creating any service."""
    calls = []
    monkeypatch.setattr(native.subprocess, "Popen", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(
        native,
        "source_state",
        lambda *a: {"head": setup.binding["source_head"], "tree": setup.binding["source_tree"]},
    )
    assert (
        native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy) is None
    )
    return calls


def receipt_fixture(setup, monkeypatch):
    """Build explicit native evidence fixtures, never qualifying a production receipt."""
    launch_fixture(setup, monkeypatch)
    launch = native.read_json(setup.directory / "launch.json")
    receipt = {
        "schema": "skfleet.native-test-receipt/v1",
        "binding": setup.binding,
        "plan_sha256": setup.digest,
        "invocation": "f" * 32,
        "pid": 123,
        "source_before": native.source_state(setup.workspace, setup.binding),
        "source_after": native.source_state(setup.workspace, setup.binding),
        "checks": [],
    }
    for check in setup.plan["checks"]:
        raw = b"fixture output\n"
        private_bytes(setup.directory / (check["id"] + ".log"), raw)
        receipt["checks"].append(
            {
                **check,
                "exit_code": 0,
                "output_sha256": native.sha(raw),
                "sandbox_argv": worker.sandbox_command(
                    setup.workspace, setup.directory / "output", check["argv"]
                ),
            }
        )
    raw = junit()
    private_bytes(setup.directory / "pytest.xml", raw)
    receipt.update(junit_sha256=native.sha(raw), counts=native.junit_counts(raw))
    native.write_once(setup.directory / "receipt.json", receipt)
    terminal = {
        "unit": launch["unit"],
        "InvocationID": receipt["invocation"],
        "ActiveState": "inactive",
        "ExecMainCode": "1",
        "ExecMainStatus": "0",
        "ExecMainPID": "123",
        "receipt_sha256": native.sha(native.read_private(setup.directory / "receipt.json")),
    }
    native.write_once(setup.directory / "terminal.json", terminal)
    return receipt, terminal


def test_real_git_preserves_source_and_rejects_dirty_or_stale(setup):
    before = native.source_state(setup.workspace, setup.binding)
    assert before["head"] == setup.binding["source_head"]
    wrong = {**setup.binding, "source_tree": "a" * 40}
    with pytest.raises(native.TestEvidenceError, match="source"):
        native.source_state(setup.workspace, wrong)
    (setup.workspace / "untracked").write_text("not source")
    with pytest.raises(native.TestEvidenceError, match="source"):
        native.source_state(setup.workspace, setup.binding)
    (setup.workspace / "untracked").unlink()
    assert native.source_state(setup.workspace, setup.binding) == before


@pytest.mark.parametrize("key", sorted(native.BINDING_KEYS))
def test_wrong_binding_is_rejected(setup, key):
    binding = {**setup.binding, key: "d" * len(setup.binding[key])}
    with pytest.raises((native.TestEvidenceError, FileNotFoundError)):
        native.load_plan(setup.home, binding)


def test_plan_must_be_private_immutable_and_fixed(setup):
    with pytest.raises(FileExistsError):
        native.seal_plan(
            setup.home, setup.binding, setup.workspace, setup.policy, "operator", "a" * 64
        )
    setup.plan_path.chmod(0o644)
    with pytest.raises(native.TestEvidenceError, match="private"):
        native.load_plan(setup.home, setup.binding)
    setup.plan_path.chmod(0o600)
    changed = copy.deepcopy(setup.plan)
    changed["checks"][0]["argv"] = ["/bin/sh", "-c", "true"]
    private_bytes(setup.plan_path, json.dumps(changed).encode())
    with pytest.raises(native.TestEvidenceError, match="plan"):
        native.load_plan(setup.home, setup.binding)


def test_missing_plan_never_launches(tmp_path):
    binding = {
        "source_card": "89508f83",
        "source_owner": "producer",
        "source_claim_revision": "claim",
        "source_revision": "revision",
        "source_head": "a" * 40,
        "source_tree": "b" * 40,
        "criteria_sha256": "c" * 64,
    }
    assert native.run_or_read_tests(tmp_path, binding, tmp_path, {}) is None


def test_receipt_rehashes_raw_output_and_terminal_custody(setup, monkeypatch):
    receipt_fixture(setup, monkeypatch)
    result = native.validate_test_receipt(setup.home, setup.binding, setup.workspace)
    assert result["counts"]["total"] == 226
    private_bytes(setup.directory / "compile.log", b"changed")
    with pytest.raises(native.TestEvidenceError, match="raw output"):
        native.validate_test_receipt(setup.home, setup.binding, setup.workspace)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ExecMainPID", "124"),
        ("InvocationID", "e" * 32),
        ("ExecMainStatus", "1"),
        ("ExecMainCode", "2"),
        ("ActiveState", "active"),
        ("receipt_sha256", "0" * 64),
        ("unit", "other.service"),
    ],
)
def test_wrong_exact_terminal_proof_fails(setup, monkeypatch, key, value):
    _, terminal = receipt_fixture(setup, monkeypatch)
    terminal[key] = value
    private_bytes(setup.directory / "terminal.json", json.dumps(terminal).encode())
    with pytest.raises(native.TestEvidenceError, match="custody"):
        native.validate_test_receipt(setup.home, setup.binding, setup.workspace)


@pytest.mark.parametrize("tag", ["failure", "error", "skipped"])
def test_failed_errored_or_skipped_junit_fails(tag):
    root = ElementTree.fromstring(junit())
    ElementTree.SubElement(next(root.iter("testcase")), tag)
    with pytest.raises(native.TestEvidenceError, match="failed, errored or skipped"):
        native.junit_counts(ElementTree.tostring(root))


def test_junit_missing_duplicate_or_inflated_coverage_fails():
    for mutation in ("missing", "duplicate", "inflated"):
        root = ElementTree.fromstring(junit())
        suite = root.find("testsuite")
        if mutation == "missing":
            suite.remove(suite[0])
        elif mutation == "duplicate":
            suite[1].set("name", suite[0].get("name"))
        else:
            suite.set("tests", "1000")
        with pytest.raises(native.TestEvidenceError):
            native.junit_counts(ElementTree.tostring(root))


def test_lost_ack_and_repeated_launch_never_duplicate_unit(setup, monkeypatch):
    calls = launch_fixture(setup, monkeypatch)
    monkeypatch.setattr(native, "observe_terminal", lambda *args: False)
    for _ in range(3):
        assert (
            native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy)
            is None
        )
    assert len(calls) == 1
    argv = calls[0][0]
    assert "--property=CPUQuota=200%" in argv
    assert "--property=MemoryMax=3221225472" in argv
    assert "--property=TasksMax=256" in argv
    assert "--property=RuntimeMaxSec=3600" in argv


def test_launch_failure_preserves_intent_and_does_not_relaunch(setup, monkeypatch):
    monkeypatch.setattr(native, "source_state", lambda *args: {})

    def fail(*args, **kwargs):
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(native.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="acknowledgement"):
        native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy)
    assert (setup.directory / "launch.json").exists()
    monkeypatch.setattr(native, "observe_terminal", lambda *args: False)
    assert (
        native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy) is None
    )


def test_resource_defer_does_not_consume_attempt(setup, monkeypatch):
    monkeypatch.setattr(native, "local_worker_admission", lambda *args: (False, "memory"))
    assert (
        native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy) is None
    )
    assert not (setup.directory / "launch.json").exists()


def test_active_resource_query_includes_builder_test_and_pi_units(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            stdout="skfleet-builder-a.service loaded active running test\n"
            "skfleet-worker-b.service loaded activating start worker\n"
        )

    monkeypatch.setattr(resources.subprocess, "run", run)
    assert len(resources.active_resource_units(Path("/nonexistent-test-home"))) == 2
    assert "skfleet-builder-*.service" in calls[0]
    assert "skfleet-worker-*.service" in calls[0]
    assert "--state=active,activating,deactivating" in calls[0]


def test_sandbox_has_no_network_secrets_or_source_writes(tmp_path):
    argv = worker.sandbox_command(tmp_path / "source", tmp_path / "output", ["python", "-V"])
    assert "--unshare-all" in argv and "--clearenv" in argv
    assert argv[argv.index(str(tmp_path / "source")) - 1] == "--ro-bind"
    assert argv.count("--bind") == 1
    assert "usr/bin" in argv and "/bin" in argv
    assert str(Path.home() / ".skcapstone") not in argv
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in argv
    assert argv[argv.index("RUFF_CACHE_DIR") + 1] == "/tmp/ruff-cache"


def test_capture_records_actual_exit_and_bounded_raw_output(tmp_path, monkeypatch):
    log = tmp_path / "actual.log"
    assert (
        worker.capture([sys.executable, "-c", "print('actual'); raise SystemExit(7)"], log, 5) == 7
    )
    assert log.read_bytes() == b"actual\n"
    monkeypatch.setattr(worker, "MAX_OUTPUT", 10)
    with pytest.raises(native.TestEvidenceError, match="output bound"):
        worker.capture([sys.executable, "-c", "print('x'*100)"], tmp_path / "large.log", 5)


def test_capture_timeout_kills_and_reaps_child(tmp_path):
    with pytest.raises(native.TestEvidenceError, match="runtime bound"):
        worker.capture(
            [sys.executable, "-c", "import time; time.sleep(10)"], tmp_path / "timeout.log", 0.05
        )


def test_evidence_symlinks_and_hardlinks_are_rejected(tmp_path):
    tmp_path.chmod(0o700)
    source = tmp_path / "source"
    private_bytes(source, b"evidence")
    (tmp_path / "symlink").symlink_to(source)
    with pytest.raises(OSError):
        native.read_private(tmp_path / "symlink")
    os.link(source, tmp_path / "hardlink")
    with pytest.raises(native.TestEvidenceError, match="private"):
        native.read_private(source)


@pytest.mark.parametrize("exit_code", [False, True, 1, -9])
def test_boolean_and_failed_command_status_cannot_be_success(setup, monkeypatch, exit_code):
    receipt, terminal = receipt_fixture(setup, monkeypatch)
    receipt["checks"][0]["exit_code"] = exit_code
    private_bytes(setup.directory / "receipt.json", json.dumps(receipt).encode())
    terminal["receipt_sha256"] = native.sha(native.read_private(setup.directory / "receipt.json"))
    private_bytes(setup.directory / "terminal.json", json.dumps(terminal).encode())
    with pytest.raises(native.TestEvidenceError, match="command"):
        native.validate_test_receipt(setup.home, setup.binding, setup.workspace)


def test_worker_receipt_requires_actual_native_invocation(setup, monkeypatch):
    launch_fixture(setup, monkeypatch)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    with pytest.raises(native.TestEvidenceError, match="native systemd"):
        worker.execute(setup.plan_path, setup.directory)


@pytest.mark.parametrize("failed", [False, True])
def test_worker_records_all_actual_check_exits_and_junit(setup, monkeypatch, failed):
    launch_fixture(setup, monkeypatch)
    monkeypatch.setenv("INVOCATION_ID", "f" * 32)
    monkeypatch.setattr(worker, "source_state", native.source_state)
    monkeypatch.setattr(worker.resource, "setrlimit", lambda *args: None)
    calls = []

    def capture(argv, path, timeout):
        calls.append(argv)
        private_bytes(path, b"fixture captured output")
        if len(calls) == 1:
            private_bytes(setup.directory / "output/pytest.xml", junit())
        return 1 if failed and len(calls) == 2 else 0

    monkeypatch.setattr(worker, "capture", capture)
    original_umask = os.umask(0o077)
    try:
        assert worker.execute(setup.plan_path, setup.directory) == int(failed)
    finally:
        os.umask(original_umask)
    receipt = native.read_json(setup.directory / "receipt.json")
    assert len(calls) == (2 if failed else 4)
    assert receipt["checks"][-1]["exit_code"] == int(failed)
    assert receipt["counts"]["total"] == 226
    assert receipt["pid"] == os.getpid()
    assert native.read_private(setup.directory / "pytest.xml") == junit()


def test_terminal_observation_checks_actual_invocation_and_pid(setup, monkeypatch):
    receipt, _ = receipt_fixture(setup, monkeypatch)
    (setup.directory / "terminal.json").unlink()
    launch = native.read_json(setup.directory / "launch.json")

    def response(pid):
        return SimpleNamespace(
            stdout="LoadState=loaded\nActiveState=inactive\n"
            "InvocationID="
            + receipt["invocation"]
            + "\nExecMainPID="
            + str(pid)
            + "\nExecMainCode=1\nExecMainStatus=0\n"
        )

    monkeypatch.setattr(native.subprocess, "run", lambda *a, **kw: response(999))
    with pytest.raises(native.TestEvidenceError, match="custody"):
        native.observe_terminal(setup.directory, launch)
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **kw: response(receipt["pid"]))
    assert native.observe_terminal(setup.directory, launch)
    assert (
        native.validate_test_receipt(setup.home, setup.binding, setup.workspace)["counts"]["total"]
        == 226
    )


def test_pending_test_launch_reserves_memory_before_unit_ack(setup, monkeypatch):
    launch_fixture(setup, monkeypatch)
    monkeypatch.setattr(resources.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=""))
    rows = resources.active_resource_units(setup.home)
    assert len(rows) == 1 and rows[0]["reserved_memory_max"] == 3 * 1024**3
    unit = rows[0]["unit"]
    monkeypatch.setattr(
        Path, "read_text", lambda *a, **kw: "MemTotal: 8388608 kB\nMemAvailable: 5242880 kB\n"
    )

    def runner(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="Id="
            + unit
            + "\nActiveState=inactive\nMemoryMax=infinity\nMemoryCurrent=[not set]\n",
        )

    ready, _ = resources.local_worker_admission(
        setup.policy, setup.plan["host"], rows, runner=runner
    )
    assert not ready
