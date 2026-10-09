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
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import production_admission as admission
from skcapstone.fleet import production_resources as resources
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profile
from skcapstone.fleet import production_test_worker as worker
from skcapstone.fleet import production_tests as native
from skcapstone.fleet.production_builder import digest as policy_digest


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
def setup(tmp_path, monkeypatch, qualified_runtime):
    """Use a real clean Git source and private immutable plan fixture."""
    workspace = tmp_path / "source"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "source.py").write_text("value = 1\n")
    # These profile fixtures now owe real source targets before plan sealing.
    for name in (
        "tests/test_parser-case.py",
        "tests/test_any.py",
        "tests/test_parser.py",
        "tests/fleet/test_parser.py",
        "scripts/fleet/skfleet-working.py",
        "src/parser-case.py",
    ):
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Synthetic sealed source target.\n")
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

    criteria = ["Run the source parser checks."]
    binding = {
        "source_card": "89508f83",
        "source_owner": "producer",
        "source_claim_revision": "claim",
        "source_head": revision("HEAD"),
        "source_tree": revision("HEAD^{tree}"),
        "source_revision": "revision",
        "criteria_sha256": policy_digest(criteria),
    }
    home = tmp_path / "home"
    (home / "fleet").mkdir(parents=True)
    CardStore(home).create(
        CardCore(
            id=binding["source_card"],
            title="Synthetic native test producer",
            acceptance_criteria=criteria,
            meta={"repository": "https://example.invalid/repo"},
            initial_owner=binding["source_owner"],
            initial_claim_revision=binding["source_claim_revision"],
        )
    )
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
    monkeypatch.setattr(admission, "active_resource_units", lambda *args: [])
    monkeypatch.setattr(admission, "local_worker_admission", lambda *args: (True, "fixture"))
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
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "exited",
        "MainPID": "0",
        "ControlGroup": "",
        "TasksCurrent": "[not set]",
        "ExecMainCode": "1",
        "ExecMainStatus": "0",
        "ExecMainPID": "123",
        "receipt_sha256": native.sha(native.read_private(setup.directory / "receipt.json")),
    }
    native.write_once(setup.directory / "terminal.json", terminal)
    return receipt, terminal


def test_fast_success_accounts_admission_before_stop_and_revalidates_product(setup, monkeypatch):
    """Fast success discharges the consumed intent before retained-unit removal."""
    _, terminal = receipt_fixture(setup, monkeypatch)
    launch = native.read_json(setup.directory / "launch.json")
    identity = launch["service_argv"][1].split("=", 2)[2]
    directory = setup.home / "fleet/resource-admission" / setup.plan["host"] / identity
    state = dict(
        terminal,
        Id=launch["unit"],
        ControlPID="0",
        Result="success",
        MemoryMax=str(launch["production"]["resources"]["memory_max_bytes"]),
        MemoryCurrent="[not set]",
        SKFLEET_ADMISSION_ID=identity,
    )
    monkeypatch.setattr(admission, "unit_state", lambda *a, **kw: dict(state))
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: False if str(p) == "/proc/123" else exists(p))
    calls = []

    def stop(*args):
        assert (directory / "success-terminal.json").exists()
        calls.append("stop")

    monkeypatch.setattr(native, "stop_retained", stop)
    # A valid resource receipt never bypasses separate product evidence checks.
    private_bytes(setup.directory / "pytest.xml", b"invalid product evidence")
    with pytest.raises(native.TestEvidenceError):
        native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy)
    assert calls == ["stop"]
    assert (directory / "intent.json").exists()
    assert not (directory / "observed.json").exists()


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


def test_source_state_allows_only_owned_generated_test_caches(setup):
    before = native.source_state(setup.workspace, setup.binding)
    exclude = setup.workspace / ".git/info/exclude"
    exclude.write_text("/.pytest_cache/\n/.ruff_cache/\n**/__pycache__/\n/.coverage\n")
    caches = {
        ".pytest_cache/v/cache/nodeids": "{}\n",
        ".ruff_cache/0.16.3/cache": "cache\n",
        "tests/__pycache__/parser.pyc": "bytecode\n",
    }
    for relative, contents in caches.items():
        path = setup.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    assert native.source_state(setup.workspace, setup.binding) == before

    (setup.workspace / ".coverage").write_text("unexpected test artifact")
    with pytest.raises(native.TestEvidenceError, match="source"):
        native.source_state(setup.workspace, setup.binding)


def test_source_state_rejects_symlinks_inside_generated_cache_paths(setup):
    exclude = setup.workspace / ".git/info/exclude"
    exclude.write_text("**/__pycache__/\n")
    cache = setup.workspace / "tests/__pycache__"
    cache.mkdir(parents=True)
    target = setup.workspace.parent / "outside-cache-target"
    target.write_text("must not be followed")
    (cache / "unsafe.pyc").symlink_to(target)

    with pytest.raises(native.TestEvidenceError, match="source"):
        native.source_state(setup.workspace, setup.binding)


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


def test_stale_executor_plan_runs_under_same_recipe_for_requalification(setup, monkeypatch):
    from skcoord.card_store import CardStore

    card = CardStore(setup.home).fold(setup.binding["source_card"])
    core = card.model_dump(mode="json")
    recipe = {"pytest": {"tests/test_parser.py": 1}, "compile": [], "lint": [], "changelog": False}
    original = profile.qualify_profile(
        setup.home,
        core,
        setup.policy,
        recipe,
        "operator",
        "b" * 64,
        source_sha256=plan.source_fingerprint(
            core["meta"]["repository"],
            setup.binding["source_head"],
            setup.binding["source_tree"],
        ),
    )
    value, predecessor = profile.read_profile(setup.home, core["id"])
    _, _, plan_predecessor = plan.load_plan(setup.home, setup.binding)
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "d" * 64)
    assert profile.fingerprint_only_stale(value, profile.contract(core), setup.policy)
    path = plan.seal_plan(
        setup.home,
        setup.binding,
        setup.workspace,
        setup.policy,
        value["qualified_by"],
        value["qualification_sha256"],
        profile=value,
        predecessor_sha256=plan_predecessor,
        requalification=True,
        profile_predecessor_sha256=predecessor,
    )
    sealed, _, _ = plan.load_plan(setup.home, setup.binding)
    assert sealed["profile_requalification"] is True
    assert sealed["profile_predecessor_sha256"] == predecessor
    assert sealed["profile"] == value
    assert sealed["checks"] == profile.recipe_checks(recipe)
    assert original.exists()
    assert path.exists()


def test_remote_initial_profile_plan_is_sealed_without_publishing_profile(setup, monkeypatch):
    from skcoord.card_store import CardStore

    setup.plan_path.unlink()
    card = CardStore(setup.home).fold(setup.binding["source_card"])
    core = card.model_dump(mode="json")
    recipe = {"pytest": {"tests/test_parser.py": 1}, "compile": [], "lint": [], "changelog": False}
    source_sha = plan.workspace_source_fingerprint(core["meta"]["repository"], setup.workspace)
    provisional = profile.profile_value(
        core,
        setup.policy,
        recipe,
        "niobe",
        "b" * 64,
        source_sha256=source_sha,
    )
    setup.policy["node_quotas"]["chiap01"] = setup.policy["node_quotas"][
        setup.policy["authority_host"]
    ]
    path = plan.seal_plan(
        setup.home,
        setup.binding,
        setup.workspace,
        setup.policy,
        "niobe",
        "b" * 64,
        profile=provisional,
        execution_host="chiap01",
        initial_profile_qualification=True,
    )
    monkeypatch.setattr(plan.socket, "gethostname", lambda: "chiap01")

    sealed, _, _ = plan.load_plan(setup.home, setup.binding, allow_remote_host=True)

    assert sealed["schema"] == "skfleet.native-test-plan/v6"
    assert sealed["remote_initial_profile_qualification"] is True
    assert sealed["profile"] == provisional
    assert not (setup.home / "fleet/test-profiles" / (core["id"] + ".json")).exists()
    assert path.exists()


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


def test_authority_waits_for_remote_requalification_receipt(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_plan as test_plan

    binding = {
        "source_card": "89508f83",
        "source_owner": "producer",
        "source_claim_revision": "claim",
        "source_head": "a" * 40,
        "source_tree": "b" * 40,
        "source_revision": "revision",
        "criteria_sha256": "c" * 64,
    }
    authority = "chiap08"
    execution = "chiap01"
    policy = {
        "authority_host": authority,
        "node_quotas": {execution: {"memory_max_bytes": 1}},
    }
    sealed = {
        "host": execution,
        "remote_requalification": True,
        "authority_host": authority,
        "policy_sha256": "d" * 64,
    }
    directory = tmp_path / "remote-run"
    monkeypatch.setattr(
        native, "load_plan", lambda *_a, **_k: (sealed, tmp_path / "plan", "e" * 64)
    )
    monkeypatch.setattr(test_plan, "execution_policy_fingerprint", lambda *_a: "d" * 64)
    monkeypatch.setattr(native, "source_state", lambda *_a: {})
    monkeypatch.setattr(native, "run_directory", lambda *_a: directory)
    monkeypatch.setattr(native.socket, "gethostname", lambda: authority)
    assert native.run_or_read_tests(tmp_path, binding, tmp_path, policy) is None

    directory.mkdir()
    (directory / "receipt.json").write_text("{}")
    receipt = {"receipt_sha256": "f" * 64}
    monkeypatch.setattr(native, "validate_test_receipt", lambda *_a: receipt)
    monkeypatch.setattr(
        native,
        "reserve_launch",
        lambda *_a, **_k: pytest.fail("authority must never launch a remote plan locally"),
    )
    assert native.run_or_read_tests(tmp_path, binding, tmp_path, policy) == receipt


def test_authority_waits_for_remote_initial_qualification_receipt(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_plan as test_plan

    binding = {
        "source_card": "89508f83",
        "source_owner": "producer",
        "source_claim_revision": "claim",
        "source_head": "a" * 40,
        "source_tree": "b" * 40,
        "source_revision": "revision",
        "criteria_sha256": "c" * 64,
    }
    authority = "chiap08"
    execution = "chiap01"
    policy = {
        "authority_host": authority,
        "node_quotas": {execution: {"memory_max_bytes": 1}},
    }
    sealed = {
        "host": execution,
        "remote_initial_profile_qualification": True,
        "initial_profile_qualification": True,
        "authority_host": authority,
        "policy_sha256": "d" * 64,
    }
    directory = tmp_path / "remote-run"
    monkeypatch.setattr(
        native, "load_plan", lambda *_a, **_k: (sealed, tmp_path / "plan", "e" * 64)
    )
    monkeypatch.setattr(test_plan, "execution_policy_fingerprint", lambda *_a: "d" * 64)
    monkeypatch.setattr(native, "source_state", lambda *_a: {})
    monkeypatch.setattr(native, "run_directory", lambda *_a: directory)
    monkeypatch.setattr(native.socket, "gethostname", lambda: authority)
    assert native.run_or_read_tests(tmp_path, binding, tmp_path, policy) is None

    directory.mkdir()
    (directory / "receipt.json").write_text("{}")
    receipt = {"receipt_sha256": "f" * 64}
    monkeypatch.setattr(native, "validate_test_receipt", lambda *_a: receipt)
    monkeypatch.setattr(
        native,
        "reserve_launch",
        lambda *_a, **_k: pytest.fail("authority must never launch a remote plan locally"),
    )
    assert native.run_or_read_tests(tmp_path, binding, tmp_path, policy) == receipt


def test_receipt_rehashes_raw_output_and_terminal_custody(setup, monkeypatch):
    receipt_fixture(setup, monkeypatch)
    result = native.validate_test_receipt(setup.home, setup.binding, setup.workspace)
    assert result["counts"]["total"] == 226
    private_bytes(setup.directory / "compile.log", b"changed")
    with pytest.raises(native.TestEvidenceError, match="raw output"):
        native.validate_test_receipt(setup.home, setup.binding, setup.workspace)


def test_directory_receipt_rehashes_exact_collection_evidence(setup, monkeypatch):
    """The independent receipt reader rejects changed collection bytes."""
    from skcapstone.fleet.production_test_profile import recipe_checks

    approved = {"pytest": {"tests": 226}, "compile": [], "lint": [], "changelog": False}
    setup.plan["profile"] = {"recipe": approved}
    setup.plan["checks"] = recipe_checks(approved)
    monkeypatch.setattr(
        native, "load_plan", lambda *a, **kw: (setup.plan, setup.plan_path, setup.digest)
    )
    receipt, terminal = receipt_fixture(setup, monkeypatch)
    selected = []
    for index, filename in enumerate(native.TEST_FILES):
        selected.extend(f"{filename}::test_{n}" for n in range(19 if index < 10 else 18))
    selection = {
        "schema": "skfleet.pytest-selection/v1",
        "baseline": [],
        "deselected": [],
        "selected": sorted(selected),
    }
    raw = json.dumps(selection).encode()
    private_bytes(setup.directory / "selection.json", raw)
    receipt["selection_sha256"] = native.sha(raw)
    receipt["counts"] = native.junit_counts(junit(), setup.plan["profile"], selection=selection)
    check = setup.plan["checks"][0]
    receipt["checks"][0]["sandbox_argv"] = worker.sandbox_command(
        setup.workspace, setup.directory / "output", check["argv"], setup.plan["profile"]
    )
    private_bytes(setup.directory / "receipt.json", json.dumps(receipt).encode())
    terminal["receipt_sha256"] = native.sha(native.read_private(setup.directory / "receipt.json"))
    private_bytes(setup.directory / "terminal.json", json.dumps(terminal).encode())
    assert (
        native.validate_test_receipt(setup.home, setup.binding, setup.workspace)["counts"]["total"]
        == 226
    )
    private_bytes(setup.directory / "selection.json", raw + b" ")
    with pytest.raises(native.TestEvidenceError, match="selection evidence changed"):
        native.validate_test_receipt(setup.home, setup.binding, setup.workspace)


def test_receipt_rebuilds_command_with_sealed_successor_path(setup, monkeypatch):
    receipt_fixture(setup, monkeypatch)
    successor = setup.plan_path.with_name(setup.plan_path.stem + ".successor.json")
    monkeypatch.setattr(
        native, "load_plan", lambda *args, **kwargs: (setup.plan, successor, setup.digest)
    )

    def exact_command(home, policy, host, unit, binding, argv):
        assert str(successor) in argv
        return native.read_json(setup.directory / "launch.json")["service_argv"]

    monkeypatch.setattr(native, "reserved_command", exact_command)
    receipt = native.validate_test_receipt(setup.home, setup.binding, setup.workspace)
    assert receipt["counts"]["total"] == 226


@pytest.mark.parametrize(
    "key,value",
    [
        ("ExecMainPID", "124"),
        ("InvocationID", "e" * 32),
        ("ExecMainStatus", "1"),
        ("ExecMainCode", "2"),
        ("ActiveState", "inactive"),
        ("SubState", "running"),
        ("MainPID", "123"),
        ("ControlGroup", "/user.slice/live"),
        ("TasksCurrent", "unknown"),
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
    with pytest.raises(admission.AdmissionError, match="launch custody unavailable"):
        native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy)
    assert (setup.directory / "launch.json").exists()
    monkeypatch.setattr(native, "observe_terminal", lambda *args: False)
    assert (
        native.run_or_read_tests(setup.home, setup.binding, setup.workspace, setup.policy) is None
    )


def test_resource_defer_does_not_consume_attempt(setup, monkeypatch):
    monkeypatch.setattr(admission, "local_worker_admission", lambda *args: (False, "memory"))
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


@pytest.mark.parametrize("failure", ["missing", "timeout", "overflow"])
def test_worker_failure_preserves_typed_receipt(setup, monkeypatch, failure):
    launch_fixture(setup, monkeypatch)
    monkeypatch.setenv("INVOCATION_ID", "f" * 32)
    monkeypatch.setattr(worker, "source_state", native.source_state)
    monkeypatch.setattr(worker.resource, "setrlimit", lambda *args: None)

    def capture(argv, path, timeout):
        private_bytes(path, b"actual failure boundary fixture")
        if failure != "missing":
            raise native.TestEvidenceError("native check exceeded " + failure)
        return 0

    monkeypatch.setattr(worker, "capture", capture)
    original_umask = os.umask(0o077)
    try:
        assert worker.execute(setup.plan_path, setup.directory) == 1
    finally:
        os.umask(original_umask)
    receipt = native.read_json(setup.directory / "receipt.json")
    assert receipt["failure"]["type"] == (
        "FileNotFoundError" if failure == "missing" else "TestEvidenceError"
    )
    assert receipt["counts"] is None and receipt["junit_sha256"] is None
    assert receipt["checks"][0]["output_sha256"]
    assert receipt["invocation"] == "f" * 32


def test_terminal_observation_checks_actual_invocation_and_pid(setup, monkeypatch):
    receipt, _ = receipt_fixture(setup, monkeypatch)
    (setup.directory / "terminal.json").unlink()
    launch = native.read_json(setup.directory / "launch.json")

    def response(pid):
        return SimpleNamespace(
            stdout="LoadState=loaded\nActiveState=active\nSubState=exited\n"
            "MainPID=0\nControlGroup=\nTasksCurrent=[not set]\n"
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
