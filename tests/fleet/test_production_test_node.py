"""Node contracts reject incomplete evidence and unqualified executable inputs."""

import copy
import json
import platform
from xml.etree import ElementTree as ET

import pytest

from skcapstone.fleet import production_test_node as node
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profile


def specimen():
    """Return a minimal real Vitest-shaped two-file report and contract."""
    value = {"schema": node.SCHEMA,
             "recipe": {"vitest": {"src/a.test.ts": 1, "src/b.test.tsx": 1}}}
    root = ET.Element("testsuites", tests="2", failures="0", errors="0")
    for name in value["recipe"]["vitest"]:
        suite = ET.SubElement(root, "testsuite", name=name, tests="1", errors="0",
                              failures="0", skipped="0")
        ET.SubElement(suite, "testcase", classname=name, name="works")
    return value, root


def test_fixed_node_commands_and_exact_membership():
    value, root = specimen()
    checks = profile.recipe_checks(value["recipe"])
    assert [c["id"] for c in checks] == ["vitest", "typecheck", "lint"]
    assert all(c["argv"][0] == "/usr/bin/node" for c in checks)
    assert "--no-cache" in checks[0]["argv"]
    assert plan.junit_counts(ET.tostring(root), value)["total"] == 2


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unexpected", "skip", "fail",
    "error", "undercount", "suite-failure", "root-total", "class", "identity", "nested",
    "zero", "suite-total", "suite-missing-count"])
def test_node_junit_negatives(mutation):
    value, root = specimen()
    suite, case = root[0], root[0][0]
    if mutation == "missing":
        root.remove(root[1])
    elif mutation == "duplicate":
        root.append(copy.deepcopy(suite))
    elif mutation == "unexpected":
        suite.set("name", "src/other.test.ts")
    elif mutation in {"skip", "fail", "error"}:
        ET.SubElement(case, {"skip": "skipped", "fail": "failure", "error": "error"}[mutation])
    elif mutation == "undercount":
        value["recipe"]["vitest"]["src/a.test.ts"] = 2
    elif mutation == "suite-failure":
        ET.SubElement(suite, "failure")
    elif mutation == "root-total":
        root.set("tests", "100")
    elif mutation == "class":
        case.set("classname", "other")
    elif mutation == "identity":
        suite.append(copy.deepcopy(case))
        suite.set("tests", "2")
    elif mutation == "nested":
        suite.append(ET.Element("testsuite"))
    elif mutation == "zero":
        suite.remove(case)
        suite.set("tests", "0")
    elif mutation == "suite-total":
        suite.set("tests", "19")
    else:
        del suite.attrib["skipped"]
    with pytest.raises(plan.TestEvidenceError):
        plan.junit_counts(ET.tostring(root), value)


@pytest.mark.parametrize("raw", [b"<", b"<!DOCTYPE x><testsuites/>", b"x" * (plan.MAX_OUTPUT + 1)])
def test_node_junit_bounded_and_parse_errors(raw):
    with pytest.raises(plan.TestEvidenceError):
        plan.junit_counts(raw, specimen()[0])


@pytest.mark.parametrize("path", ["../a.test.ts", "src/a.test.ts;id", "/src/a.test.ts",
                                  "src//a.test.ts", "src/../a.test.ts", "--help"])
def test_node_recipe_never_accepts_arbitrary_commands(path):
    with pytest.raises(plan.TestEvidenceError):
        node.checks({"vitest": {path: 1}})


def test_dependency_artifact_and_runtime_drift(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    artifact = root / "staging"
    (artifact / "node_modules").mkdir(parents=True)
    (artifact / "apps/web/node_modules").mkdir(parents=True)
    module = artifact / "node_modules/a.js"
    module.write_text("public synthetic module")
    module.chmod(0o444)
    for p in sorted(artifact.rglob("*"), reverse=True):
        if p.is_dir():
            p.chmod(0o555)
    artifact.chmod(0o555)
    digest = node.artifact_digest(artifact)
    artifact.rename(root / digest)
    artifact = root / digest
    monkeypatch.setattr(node, "ARTIFACT_ROOT", root)
    executable = tmp_path / "node"
    executable.write_bytes(b"runtime")
    monkeypatch.setattr(node, "NODE", executable)
    environment = {"node_sha256": plan.sha(b"runtime"), "artifact_sha256": digest,
                   "platform": [platform.system(), platform.machine()],
                   "source_sha256": dict.fromkeys(node.SOURCE_FILES, "a" * 64)}
    node.validate_environment(environment)
    executable.write_bytes(b"changed")
    with pytest.raises(plan.TestEvidenceError, match="environment"):
        node.validate_environment(environment)
    executable.write_bytes(b"runtime")
    module = artifact / "node_modules/a.js"
    module.chmod(0o644)
    with pytest.raises(plan.TestEvidenceError, match="writable"):
        node.validate_environment(environment)
    module.write_text("drift")
    module.chmod(0o444)
    with pytest.raises(plan.TestEvidenceError, match="dependencies"):
        node.validate_environment(environment)
    module.parent.chmod(0o755)
    (module.parent / "escape").symlink_to("/etc/passwd")
    module.parent.chmod(0o555)
    with pytest.raises(plan.TestEvidenceError, match="escapes"):
        node.artifact_digest(artifact)


def test_source_config_scripts_and_lock_must_match(tmp_path, monkeypatch):
    monkeypatch.setattr(node, "artifact_path", lambda e: tmp_path)
    monkeypatch.setattr(node, "artifact_digest", lambda p: "a" * 64)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "apps/web/node_modules").mkdir(parents=True)
    for relative in node.SOURCE_FILES:
        (tmp_path / relative).write_text("{}")
    package = tmp_path / "apps/web/package.json"
    scripts = {"test": "vitest run", "typecheck": "tsc -b", "lint": "eslint ."}
    package.write_text(json.dumps({"scripts": scripts}))
    environment = {"node_sha256": plan.sha(node.NODE.read_bytes()), "artifact_sha256": "a" * 64,
                   "platform": [platform.system(), platform.machine()],
                   "source_sha256": {
                       p: plan.sha((tmp_path / p).read_bytes()) for p in node.SOURCE_FILES}}
    node.validate_environment(environment, tmp_path)
    (tmp_path / "package-lock.json").write_text("changed")
    with pytest.raises(plan.TestEvidenceError, match="configuration"):
        node.validate_environment(environment, tmp_path)
    (tmp_path / "package-lock.json").write_text("{}")
    scripts["pretest"] = "arbitrary"
    package.write_text(json.dumps({"scripts": scripts}))
    environment["source_sha256"]["apps/web/package.json"] = plan.sha(package.read_bytes())
    with pytest.raises(plan.TestEvidenceError, match="scripts"):
        node.validate_environment(environment, tmp_path)


@pytest.mark.parametrize("kind", ["broken", "cycle"])
def test_artifact_invalid_links_are_typed_failures(tmp_path, kind):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "link").symlink_to("missing" if kind == "broken" else "link")
    artifact.chmod(0o555)
    with pytest.raises(plan.TestEvidenceError, match="broken or cyclic"):
        node.artifact_digest(artifact)


@pytest.mark.parametrize("relative", ["outside", "apps", "apps/web", "apps/web/socket"])
def test_source_scaffold_rejects_host_redirects(tmp_path, monkeypatch, relative):
    from skcapstone.fleet import production_test_worker as worker
    workspace = tmp_path / "source"
    (workspace / "apps/web").mkdir(parents=True)
    target = workspace / relative
    if target.is_dir():
        import shutil
        shutil.rmtree(target)
    target.symlink_to("/run/user/1000/private-socket")
    monkeypatch.setattr(node, "artifact_path", lambda value: tmp_path / "artifact")
    with pytest.raises(plan.TestEvidenceError, match="redirected"):
        worker.sandbox_command(workspace, tmp_path / "output", ["node"],
                               {"schema": node.SCHEMA, "node_environment": {}})


def test_junit_encoded_entities_and_root_skips_are_rejected():
    value, root = specimen()
    root.set("skipped", "1")
    with pytest.raises(plan.TestEvidenceError):
        node.junit_counts(ET.tostring(root), value)
    with pytest.raises(plan.TestEvidenceError):
        node.junit_counts('<!DOCTYPE x [<!ENTITY a "x">]><testsuites/>'.encode('utf-16'), value)


def test_only_typecheck_gets_its_private_build_info_mount(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_worker as worker
    workspace = tmp_path / "source"
    (workspace / "apps/web").mkdir(parents=True)
    monkeypatch.setattr(node, "artifact_path", lambda value: tmp_path / "artifact")
    value, _ = specimen()
    value["node_environment"] = {}
    for check in node.checks(value["recipe"]):
        argv = worker.sandbox_command(workspace, tmp_path / "output", check["argv"], value)
        assert ("/work/apps/web/tsconfig.tsbuildinfo" in argv) == (check["id"] == "typecheck")
        assert argv[argv.index("--remount-ro") + 1] == "/work"
