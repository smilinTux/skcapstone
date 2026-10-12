"""Native Node coverage is proven by sealed per-file commands and raw reports."""

import json
import platform
import subprocess

import pytest

from skcapstone.fleet import baseline_node_recipe as baseline
from skcapstone.fleet import production_node_test_recipe as recipe
from skcapstone.fleet import production_test_composite as composite
from skcapstone.fleet import production_test_node as node
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profiles
from skcapstone.fleet import production_test_worker as worker
from skcapstone.fleet import production_tests as native

from .test_production_tests import private_bytes, receipt_fixture
from .test_production_tests import setup as setup  # noqa: F401


@pytest.fixture
def source(tmp_path, monkeypatch):
    """Two native files with the same case name and a root dependency mount."""
    workspace = tmp_path / "native-source"
    (workspace / "tests").mkdir(parents=True)
    for path in recipe.SOURCE_FILES:
        (workspace / path).write_text("" if path.endswith("mjs") else "{}")
    for name in ("a", "metrics-delivery"):
        (workspace / f"tests/{name}.test.mjs").write_text(
            "import { test } from 'node:test'; test('same', () => {});"
        )
    artifact = tmp_path / "artifact"
    (artifact / "node_modules").mkdir(parents=True)
    for name in ("package.json", "package-lock.json"):
        (artifact / name).write_bytes((workspace / name).read_bytes())
    monkeypatch.setattr(node, "artifact_path", lambda _: artifact)
    monkeypatch.setattr(node, "artifact_digest", lambda _: "a" * 64)
    environment = {
        "node_sha256": plan.sha(node.NODE.read_bytes()),
        "artifact_sha256": "a" * 64,
        "platform": [platform.system(), platform.machine()],
        "source_sha256": {p: plan.sha((workspace / p).read_bytes()) for p in recipe.SOURCE_FILES},
    }
    monkeypatch.setattr(node, "qualified_environment", lambda _: environment)
    value, environment = profiles.initial_recipe({"description": "Node gateway"}, workspace)
    return workspace, {"schema": node.SCHEMA, "recipe": value, "node_environment": environment}


def reports(profile):
    """Keep native file identity outside the filename-free JUnit body."""
    return dict.fromkeys(
        composite.report_names(profile),
        b'<testsuites><testcase name="same" classname="test"/></testsuites>',
    )


def test_detection_environment_and_sandbox(source, tmp_path):
    workspace, profile = source
    assert list(profile["recipe"]["node_test"]) == [
        "tests/a.test.mjs",
        "tests/metrics-delivery.test.mjs",
    ]
    composite.validate_source(profile, workspace)
    checks = profiles.recipe_checks(profile["recipe"])
    assert [c["id"] for c in checks] == ["node-000", "node-001"]
    assert checks[0]["argv"] == [
        str(node.NODE),
        "--test",
        "--import",
        "./tests/_setup.mjs",
        "--test-reporter=junit",
        "--test-reporter-destination=/output/node-000.xml",
        "tests/a.test.mjs",
    ]
    command = worker.sandbox_command(workspace, tmp_path / "output", checks[0]["argv"], profile)
    assert command[command.index("--chdir") + 1] == "/work"
    assert "--unshare-all" in command and "--clearenv" in command
    assert "/work/node_modules" in command and "/work/apps/web/node_modules" not in command
    (workspace / "tests/_setup.mjs").write_text("// changed")
    with pytest.raises(plan.TestEvidenceError, match="configuration changed"):
        composite.validate_source(profile, workspace)


def test_operator_baseline_recipe_keeps_explicit_targets(source):
    workspace, profile = source
    configured = {"node_test": {"tests/a.test.mjs": 1}}
    result, _ = profiles.initial_recipe(
        {"links": {"test_profile_recipe": json.dumps(configured)}}, workspace
    )
    assert result == configured


@pytest.mark.parametrize(
    "bad", ["../x.test.mjs", "tests/a.test.mjs;id", "tests/sub/a.test.mjs", "--help"]
)
def test_target_injection_refuses(bad):
    with pytest.raises(plan.TestEvidenceError):
        profiles.recipe_checks({"node_test": {bad: 1}})


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "failure", "skip", "empty", "entity", "count"]
)
def test_incomplete_or_bad_file_evidence_refuses(source, mutation):
    _, profile = source
    raw = reports(profile)
    if mutation == "missing":
        del raw["node-001.xml"]
    elif mutation == "extra":
        raw["node-999.xml"] = raw["node-000.xml"]
    elif mutation in {"failure", "skip"}:
        tag = b"failure" if mutation == "failure" else b"skipped"
        raw["node-001.xml"] = raw["node-001.xml"].replace(b"/>", b"><" + tag + b"/></testcase>")
    elif mutation == "empty":
        raw["node-001.xml"] = b"<testsuites/>"
    elif mutation == "entity":
        raw["node-001.xml"] = b'<!DOCTYPE testsuites [<!ENTITY x "y">]><testsuites/>'
    else:
        raw["node-001.xml"] = raw["node-001.xml"].replace(
            b"<testsuites>", b'<testsuites tests="7">'
        )
    with pytest.raises(plan.TestEvidenceError):
        composite.evidence(raw, profile)


def test_real_node_nested_junit(tmp_path):
    if not node.NODE.is_file():
        pytest.skip("Node runtime unavailable")
    target = tmp_path / "nested.test.mjs"
    target.write_text(
        "import {test,describe} from 'node:test';"
        "describe('outer',()=>{describe('inner',()=>{test('one',()=>{});test('two',()=>{});});});"
    )
    result = subprocess.run(
        [str(node.NODE), "--test", "--test-reporter=junit", str(target)],
        capture_output=True,
        check=True,
    )
    assert recipe.junit_count(result.stdout, 2) == 2


@pytest.mark.parametrize("failure", ["ERR_ASSERTION", "ERR_MODULE_NOT_FOUND", "timeout"])
def test_baseline_excludes_only_proven_assertion_file(source, tmp_path, failure):
    workspace, profile = source
    directory = tmp_path / "run"
    directory.mkdir(mode=0o700)
    checks = [
        {**check, "exit_code": index, "output_sha256": plan.sha(b"log")}
        for index, check in enumerate(recipe.checks(profile["recipe"]))
    ]
    private_bytes(directory / "node-001.log", b"log")
    private_bytes(
        directory / "node-001.xml",
        (
            '<testsuites><testcase name="bad" classname="test"><failure>'
            + failure
            + "</failure></testcase></testsuites>"
        ).encode(),
    )
    raw = baseline.failed_report(directory, checks, profile["recipe"])
    if failure == "ERR_ASSERTION":
        result = json.loads(baseline.baseline_recipe(raw, workspace, profile["recipe"]))
        assert result == {"node_test": {"tests/a.test.mjs": 1}}
    else:
        with pytest.raises(plan.TestEvidenceError, match="proven assertion"):
            baseline.baseline_recipe(raw, workspace, profile["recipe"])
    checks[-1]["argv"][-1] = "tests/a.test.mjs"
    with pytest.raises(plan.TestEvidenceError, match="prefix"):
        baseline.failed_report(directory, checks, profile["recipe"])


@pytest.mark.parametrize("tamper", [None, "report", "missing", "check", "argv"])
def test_receipt_revalidates_every_native_target(setup, source, monkeypatch, tamper):  # noqa: F811
    workspace, profile = source
    receipt, terminal = receipt_fixture(setup, monkeypatch)
    setup.workspace = workspace
    launch = native.read_json(setup.directory / "launch.json")
    launch["workspace"] = str(workspace)
    launch["service_argv"] = native.service_argv(
        launch, setup.plan_path, setup.directory, workspace
    )
    monkeypatch.setattr(native, "reserved_command", lambda *args: launch["service_argv"])
    private_bytes(setup.directory / "launch.json", json.dumps(launch).encode())
    setup.plan.update(profile=profile, checks=recipe.checks(profile["recipe"]))
    monkeypatch.setattr(
        native, "load_plan", lambda *a, **kw: (setup.plan, setup.plan_path, setup.digest)
    )
    receipt["checks"] = []
    for check in setup.plan["checks"]:
        private_bytes(setup.directory / (check["id"] + ".log"), b"log")
        receipt["checks"].append(
            {
                **check,
                "exit_code": 0,
                "output_sha256": plan.sha(b"log"),
                "sandbox_argv": worker.sandbox_command(
                    workspace, setup.directory / "output", check["argv"], profile
                ),
            }
        )
    raw = reports(profile)
    for name, value in raw.items():
        private_bytes(setup.directory / name, value)
    receipt["junit_sha256"], receipt["counts"] = composite.evidence(raw, profile)
    if tamper == "report":
        private_bytes(setup.directory / "node-001.xml", raw["node-001.xml"] + b" ")
    elif tamper == "missing":
        (setup.directory / "node-001.xml").unlink()
    elif tamper == "check":
        receipt["checks"].pop()
    elif tamper == "argv":
        receipt["checks"][-1]["argv"] = receipt["checks"][0]["argv"]
    private_bytes(setup.directory / "receipt.json", json.dumps(receipt).encode())
    terminal["receipt_sha256"] = plan.sha(plan.read_private(setup.directory / "receipt.json"))
    private_bytes(setup.directory / "terminal.json", json.dumps(terminal).encode())
    if tamper is None:
        result = native.validate_test_receipt(setup.home, setup.binding, workspace)
        assert result["counts"]["per_file"] == dict.fromkeys(profile["recipe"]["node_test"], 1)
    else:
        with pytest.raises(plan.TestEvidenceError):
            native.validate_test_receipt(setup.home, setup.binding, workspace)


@pytest.mark.parametrize("fail", [False, True])
def test_worker_keeps_each_native_report_and_stops_on_failure(source, tmp_path, monkeypatch, fail):
    import os

    workspace, profile = source
    monkeypatch.setattr(worker.resource, "setrlimit", lambda *args: None)
    directory = tmp_path / "fleet/test-runs" / ("a" * 64)
    output = directory / "output"
    output.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    plan_path = tmp_path / "fleet/test-plans" / ("a" * 64 + ".json")
    plan.write_once(
        directory / "launch.json",
        {
            "binding": {"source_card": "1234abcd"},
            "workspace": str(workspace),
            "request_id": "a" * 64,
            "production": {"resources": {"runtime_max_seconds": 5}},
        },
    )
    checks = recipe.checks(profile["recipe"])
    monkeypatch.setattr(
        worker,
        "load_plan",
        lambda *args: ({"profile": profile, "checks": checks}, plan_path, "a" * 64),
    )
    monkeypatch.setattr(
        worker, "source_state", lambda *args: {"head": "retained", "tree": "retained"}
    )
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    ran = []

    def capture(argv, log, timeout):
        ran.append(log.stem)
        private_bytes(log, b"log")
        raw = reports(profile)[log.stem + ".xml"]
        if fail:
            raw = raw.replace(b"/>", b"><failure>ERR_ASSERTION</failure></testcase>")
        private_bytes(output / (log.stem + ".xml"), raw)
        return int(fail)

    monkeypatch.setattr(worker, "capture", capture)
    previous = os.umask(0o077)
    try:
        assert worker.execute(plan_path, directory) == int(fail)
    finally:
        os.umask(previous)
    receipt = plan.read_json(directory / "receipt.json")
    assert ran == (["node-000"] if fail else ["node-000", "node-001"])
    assert (directory / "node-000.xml").is_file()
    if fail:
        assert receipt["counts"] is None
        assert "node-001 was not run" in receipt["failure"]["message"]
    else:
        assert receipt["counts"]["total"] == 2


def test_native_profile_round_trip(source):
    _, profile = source
    profile.update(
        {
            key: "a" * 64
            for key in (
                "qualification_sha256",
                "python_sha256",
                "toolchain_sha256",
                "runtime_sha256",
                "criteria_sha256",
                "policy_sha256",
                "source_sha256",
            )
        }
    )
    profile.update(
        card="1234abcd",
        repository="https://example.invalid/repo",
        qualified_by="operator",
        host="test",
    )
    profiles._validate_shape(profile)
    profile["schema"] = profiles.SCHEMA
    del profile["node_environment"]
    with pytest.raises(plan.TestEvidenceError, match="variant"):
        profiles._validate_shape(profile)


@pytest.mark.parametrize("redirect", ["file", "parent", "web"])
def test_missing_or_redirected_native_inputs_refuse(source, tmp_path, redirect):
    workspace, profile = source
    if redirect == "file":
        (workspace / "tests/a.test.mjs").unlink()
        (workspace / "tests/a.test.mjs").symlink_to(workspace / "tests/metrics-delivery.test.mjs")
    elif redirect == "parent":
        (workspace / "tests").rename(tmp_path / "moved-tests")
        (workspace / "tests").symlink_to(tmp_path / "moved-tests", target_is_directory=True)
    else:
        (workspace / "apps/web").mkdir(parents=True)
    with pytest.raises(plan.TestEvidenceError):
        composite.validate_source(profile, workspace)


def test_native_environment_refuses_foreign_dependency_artifact(source):
    workspace, profile = source
    artifact = node.artifact_path(profile["node_environment"])
    (artifact / "package-lock.json").write_text('{"foreign":true}')
    with pytest.raises(plan.TestEvidenceError, match="dependency manifests"):
        composite.validate_source(profile, workspace)


def test_curator_reads_native_failure_and_waits_for_missing_artifacts(
    source, tmp_path, monkeypatch
):
    from skcapstone.fleet import baseline_recipe as curator

    workspace, profile = source
    directory = tmp_path / "fleet/test-runs" / ("a" * 64)
    directory.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(plan, "load_plan", lambda *a, **kw: ({"profile": profile}, None, "a" * 64))
    check = {
        **recipe.checks(profile["recipe"])[0],
        "exit_code": 1,
        "output_sha256": plan.sha(b"log"),
    }
    plan.write_once(directory / "receipt.json", {"checks": [check]})
    job = {"binding": {}, "workspace": str(workspace)}
    with pytest.raises(curator.BaselinePendingError, match="awaiting"):
        curator._failed_run(tmp_path, job)
    private_bytes(directory / "node-000.log", b"log")
    private_bytes(
        directory / "node-000.xml",
        b'<testsuites><testcase name="bad" classname="test">'
        b"<failure>ERR_ASSERTION</failure></testcase></testsuites>",
    )
    raw, sealed_workspace, ran = curator._failed_run(tmp_path, job)
    assert sealed_workspace == workspace and ran == profile["recipe"]
    result = json.loads(baseline.baseline_recipe(raw, workspace, ran))
    assert result == {"node_test": {"tests/metrics-delivery.test.mjs": 1}}


def test_qualification_selects_exact_native_dependency_manifests(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    for name in recipe.SOURCE_FILES:
        (workspace / name).write_text("" if name.endswith("mjs") else "{}")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    artifact = artifacts / "preparing"
    (artifact / "node_modules").mkdir(parents=True)
    for name in ("package.json", "package-lock.json"):
        (artifact / name).write_bytes((workspace / name).read_bytes())
        (artifact / name).chmod(0o400)
    (artifact / "node_modules").chmod(0o500)
    artifact.chmod(0o500)
    digest = node.artifact_digest(artifact)
    artifact.rename(artifacts / digest)
    monkeypatch.setattr(node, "ARTIFACT_ROOT", artifacts)
    environment = node.qualified_environment(workspace)
    assert environment["artifact_sha256"] == digest
    assert set(environment["source_sha256"]) == set(recipe.SOURCE_FILES)
    (workspace / "package-lock.json").write_text('{"changed":true}')
    with pytest.raises(plan.TestEvidenceError, match="no qualified Node"):
        node.qualified_environment(workspace)
