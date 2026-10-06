"""Mixed candidates owe both real Node and Python native evidence."""

import copy
import json
import os

import pytest

from skcapstone.fleet import production_test_composite as composite
from skcapstone.fleet import production_test_node as node
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profiles
from skcapstone.fleet import production_test_worker as worker
from skcapstone.fleet import production_tests as native
from skcapstone.fleet.production_pytest_recipe import recipe_checks

from .test_production_tests import private_bytes, receipt_fixture
from .test_production_tests import setup as setup


def recipe():
    """Return one bounded frontend/API contract, never model command text."""
    return {
        "vitest": {"src/corpus.test.tsx": 1},
        "pytest": {"tests/test_api_corpus.py": 1},
        "compile": ["src/corpus.py"],
        "lint": ["src/corpus.py"],
        "changelog": False,
    }


def test_composite_compiles_both_required_phases_without_id_collisions():
    checks = recipe_checks(recipe())
    assert [check["id"] for check in checks] == [
        "vitest",
        "typecheck",
        "lint",
        "pytest",
        "compile",
        "python-lint",
    ]
    assert len({check["id"] for check in checks}) == len(checks)
    assert checks[3]["argv"][-1] == "tests/test_api_corpus.py"


def specimen():
    """Return separate authentic report shapes and one explicit combined profile."""
    profile = {"schema": composite.SCHEMA, "recipe": recipe(), "node_environment": {}}
    reports = {
        "vitest.xml": (
            b'<testsuites tests="1" errors="0" failures="0">'
            b'<testsuite name="src/corpus.test.tsx" tests="1" errors="0" failures="0" skipped="0">'
            b'<testcase classname="src/corpus.test.tsx" name="denies"/></testsuite></testsuites>'
        ),
        "pytest.xml": (
            b'<testsuites><testsuite tests="1" errors="0" failures="0" skipped="0">'
            b'<testcase classname="tests.test_api_corpus" name="test_denies"/>'
            b"</testsuite></testsuites>"
        ),
    }
    return profile, reports


def test_composite_counts_both_raw_files_and_binds_both_digests():
    profile, reports = specimen()
    digest, counts = composite.evidence(reports, profile)
    assert counts["total"] == 2
    assert counts["phases"]["node"]["per_file"] == {"src/corpus.test.tsx": 1}
    assert counts["phases"]["python"]["per_file"] == {"tests/test_api_corpus.py": 1}
    changed = {**reports, "pytest.xml": reports["pytest.xml"].replace(b"test_denies", b"test_new")}
    assert composite.evidence(changed, profile)[0] != digest


@pytest.mark.parametrize("name", ["vitest.xml", "pytest.xml"])
@pytest.mark.parametrize("defect", ["missing", "failure", "skip", "wrong-file", "entity"])
def test_either_phase_defect_refuses_aggregate(name, defect):
    profile, reports = specimen()
    if defect == "missing":
        del reports[name]
    elif defect in {"failure", "skip"}:
        tag = b"failure" if defect == "failure" else b"skipped"
        reports[name] = reports[name].replace(
            b"/></testsuite>", b"><" + tag + b"/></testcase></testsuite>"
        )
    elif defect == "wrong-file":
        reports[name] = reports[name].replace(b"corpus", b"other")
    else:
        reports[name] = b'<!DOCTYPE testsuites [<!ENTITY leak "x">]>' + reports[name]
    with pytest.raises(plan.TestEvidenceError):
        composite.evidence(reports, profile)


@pytest.mark.parametrize(
    "extra", [{"argv": ["sh", "-c", "false"]}, {"vitest": {}}, {"pytest": {}}, {"compile": []}]
)
def test_incomplete_or_arbitrary_phase_recipe_refuses(extra):
    value = recipe()
    value.update(extra)
    if extra == {"compile": []}:
        del value["changelog"]
    with pytest.raises(plan.TestEvidenceError):
        recipe_checks(value)


def test_profile_variant_cannot_omit_or_disguise_a_language():
    value, _ = specimen()
    value.update(
        {
            key: "a" * 64
            for key in [
                "criteria_sha256",
                "qualification_sha256",
                "python_sha256",
                "runtime_sha256",
                "policy_sha256",
            ]
        }
    )
    value.update(
        card="1234abcd",
        repository="https://example.invalid/repo",
        qualified_by="operator",
        host="test",
    )
    profiles._validate_shape(value)
    for mutation in ("node-schema", "python-schema", "missing-python", "missing-node"):
        changed = copy.deepcopy(value)
        if mutation == "node-schema":
            changed["schema"] = node.SCHEMA
        elif mutation == "python-schema":
            changed["schema"] = profiles.SCHEMA
            del changed["node_environment"]
        elif mutation == "missing-python":
            changed["recipe"] = {"vitest": value["recipe"]["vitest"]}
        else:
            del changed["recipe"]["vitest"]
        with pytest.raises(plan.TestEvidenceError):
            profiles._validate_shape(changed)


def source_fixture(tmp_path, monkeypatch):
    """Create bounded real source for phase mount checks; no host dependencies run."""
    workspace = tmp_path / "source"
    for relative in ["apps/web/src/corpus.test.tsx", "src/corpus.py", "tests/test_api_corpus.py"]:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic\n")
    artifact = tmp_path / "artifact"
    (artifact / "node_modules").mkdir(parents=True)
    (artifact / "apps/web/node_modules").mkdir(parents=True)
    monkeypatch.setattr(node, "artifact_path", lambda _: artifact)
    monkeypatch.setattr(node, "validate_environment", lambda *args: None)
    return workspace


def test_fixed_sandboxes_use_each_language_cwd_and_clear_environment(tmp_path, monkeypatch):
    workspace = source_fixture(tmp_path, monkeypatch)
    profile, _ = specimen()
    for check in recipe_checks(profile["recipe"]):
        argv = worker.sandbox_command(workspace, tmp_path / "output", check["argv"], profile)
        node_phase = check["id"] in {"vitest", "typecheck", "lint"}
        assert argv[argv.index("--chdir") + 1] == ("/work/apps/web" if node_phase else "/work")
        assert ("/work/apps/web/node_modules" in argv) == node_phase
        assert "--clearenv" in argv and "--unshare-all" in argv
        assert "--share-net" not in argv
        assert ("/qualified/production_pytest_selection.py" in argv) != node_phase
    with pytest.raises(plan.TestEvidenceError, match="outside qualified"):
        worker.sandbox_command(workspace, tmp_path / "output", ["sh", "-c", "false"], profile)


def test_both_source_contracts_are_validated(tmp_path, monkeypatch):
    workspace = source_fixture(tmp_path, monkeypatch)
    profile, _ = specimen()
    composite.validate_source(profile, workspace)
    (workspace / "tests/test_api_corpus.py").unlink()
    with pytest.raises(plan.TestEvidenceError, match="missing"):
        composite.validate_source(profile, workspace)


@pytest.mark.parametrize("failed", [None, "vitest", "pytest", "python-lint"])
def test_native_executor_requires_every_phase_and_preserves_raw_reports(
    tmp_path, monkeypatch, failed
):
    monkeypatch.setattr(worker.resource, "setrlimit", lambda *args: None)
    workspace = source_fixture(tmp_path, monkeypatch)
    profile, reports = specimen()
    home = tmp_path / "home"
    plan_path = home / "fleet/test-plans" / ("a" * 64 + ".json")
    directory = home / "fleet/test-runs" / ("a" * 64)
    output = directory / "output"
    output.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    binding = {"source_card": "1234abcd"}
    plan.write_once(
        directory / "launch.json",
        {
            "binding": binding,
            "workspace": str(workspace),
            "request_id": "a" * 64,
            "production": {"resources": {"runtime_max_seconds": 5}},
        },
    )
    checks = recipe_checks(profile["recipe"])
    monkeypatch.setattr(
        worker,
        "load_plan",
        lambda *a: ({"profile": profile, "checks": checks}, plan_path, "a" * 64),
    )
    monkeypatch.setattr(
        worker, "source_state", lambda *a: {"head": "retained", "tree": "retained"}
    )
    monkeypatch.setenv("INVOCATION_ID", "b" * 32)
    ran = []

    def capture(argv, path, timeout):
        name = path.stem
        ran.append(name)
        plan.write_once(path, {"synthetic": name})
        if name in {"vitest", "pytest"}:
            report = output / (name + ".xml")
            report.write_bytes(reports[name + ".xml"])
            report.chmod(0o600)
        return int(name == failed)

    monkeypatch.setattr(worker, "capture", capture)
    original_umask = os.umask(0o077)
    try:
        assert worker.execute(plan_path, directory) == int(failed is not None)
    finally:
        os.umask(original_umask)
    receipt = plan.read_json(directory / "receipt.json")
    if failed is None:
        assert ran == [check["id"] for check in checks]
        digest, counts = composite.evidence(reports, profile)
        assert receipt["junit_sha256"] == digest and receipt["counts"] == counts
        for name, raw in reports.items():
            assert plan.read_private(directory / name) == raw
    else:
        assert receipt["failure"] is not None or any(row["exit_code"] for row in receipt["checks"])


@pytest.mark.parametrize(
    "tamper",
    [None, "vitest.xml", "pytest.xml", "missing-python", "skipped-python", "missing-check"],
)
def test_independent_native_acceptance_rehashes_both_phases(setup, tmp_path, monkeypatch, tamper):
    profile, reports = specimen()
    receipt, terminal = receipt_fixture(setup, monkeypatch)
    setup.workspace = source_fixture(tmp_path, monkeypatch)
    launch = native.read_json(setup.directory / "launch.json")
    launch["workspace"] = str(setup.workspace)
    launch["service_argv"] = native.service_argv(
        launch, setup.plan_path, setup.directory, setup.workspace
    )
    # Admission is covered by the shared fixture. Retain its exact controller
    # argv while testing downstream raw evidence, not a production qualification.
    monkeypatch.setattr(native, "reserved_command", lambda *args: launch["service_argv"])
    private_bytes(setup.directory / "launch.json", json.dumps(launch).encode())
    setup.plan.update(profile=profile, checks=recipe_checks(profile["recipe"]))
    monkeypatch.setattr(
        native, "load_plan", lambda *a, **kw: (setup.plan, setup.plan_path, setup.digest)
    )
    receipt["checks"] = []
    for check in setup.plan["checks"]:
        raw = b"synthetic phase stdout\n"
        private_bytes(setup.directory / (check["id"] + ".log"), raw)
        receipt["checks"].append(
            {
                **check,
                "exit_code": 0,
                "output_sha256": plan.sha(raw),
                "sandbox_argv": worker.sandbox_command(
                    setup.workspace, setup.directory / "output", check["argv"], profile
                ),
            }
        )
    for name, raw in reports.items():
        private_bytes(setup.directory / name, raw)
    receipt["junit_sha256"], receipt["counts"] = composite.evidence(reports, profile)
    if tamper in reports:
        private_bytes(setup.directory / tamper, reports[tamper] + b" ")
    elif tamper == "missing-python":
        (setup.directory / "pytest.xml").unlink()
    elif tamper == "skipped-python":
        private_bytes(
            setup.directory / "pytest.xml",
            reports["pytest.xml"].replace(b"/>", b"><skipped/></testcase>"),
        )
    elif tamper == "missing-check":
        receipt["checks"].pop()
    private_bytes(setup.directory / "receipt.json", json.dumps(receipt).encode())
    terminal["receipt_sha256"] = plan.sha(plan.read_private(setup.directory / "receipt.json"))
    private_bytes(setup.directory / "terminal.json", json.dumps(terminal).encode())
    if tamper is None:
        result = native.validate_test_receipt(setup.home, setup.binding, setup.workspace)
        assert result["counts"]["total"] == 2
    else:
        with pytest.raises(plan.TestEvidenceError):
            native.validate_test_receipt(setup.home, setup.binding, setup.workspace)
