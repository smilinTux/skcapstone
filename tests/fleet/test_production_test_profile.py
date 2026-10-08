"""Qualified contracts gate tokens and seal exact candidates without model commands."""

import ast
import copy
import json
import socket
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profile
from tests.fleet.test_production_tests import setup  # noqa: F401,F811


def _site(prefix: Path) -> Path:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return prefix / "lib" / version / "site-packages"


def test_runtime_fingerprint_tracks_changed_installed_bytes(qualified_runtime):
    before = plan.runtime_fingerprint()
    assert plan.runtime_fingerprint() == before
    (qualified_runtime / "bin/ruff").write_bytes(b"changed synthetic runtime\n")
    assert plan.runtime_fingerprint() != before


def test_runtime_fingerprint_ignores_install_provenance_and_subject_code(qualified_runtime):
    before = plan.runtime_fingerprint()
    site = _site(qualified_runtime)
    metadata = site / "skcapstone-0.15.201.dist-info"
    metadata.mkdir()
    (metadata / "RECORD").write_text("new wheel provenance\n")
    subject = site / "skcapstone"
    subject.mkdir()
    (subject / "unrelated.py").write_text("changed application code\n")
    assert plan.runtime_fingerprint() == before


def test_runtime_fingerprint_tracks_harness_and_startup_bytes(qualified_runtime):
    before = plan.runtime_fingerprint()
    (plan.HARNESS_ROOT / "production_test_worker.py").write_text("changed trusted executor\n")
    changed = plan.runtime_fingerprint()
    assert changed != before
    site = _site(qualified_runtime)
    startup = site / "__editable__.skcapstone-0.15.200.pth"
    startup.write_text("/work/source\n")
    with_startup = plan.runtime_fingerprint()
    assert with_startup != changed
    startup.rename(site / "__editable__.skcapstone-0.15.201.pth")
    assert plan.runtime_fingerprint() == with_startup
    (site / "__editable__.skcapstone-0.15.201.pth").write_text("/different/source\n")
    assert plan.runtime_fingerprint() != with_startup
    (site / "__editable__.skcapstone-0.15.200.pth").write_text("/work/source\n")
    with pytest.raises(plan.TestEvidenceError, match="ambiguous"):
        plan.runtime_fingerprint()


def test_runtime_fingerprint_tracks_test_tool_metadata(qualified_runtime):
    site = _site(qualified_runtime)
    metadata = site / "pytest-9.1.dist-info"
    metadata.mkdir()
    path = metadata / "METADATA"
    path.write_text("Version: 9.1\n")
    before = plan.runtime_fingerprint()
    path.write_text("Version: 9.2\n")
    assert plan.runtime_fingerprint() != before


def test_toolchain_fingerprint_tracks_tools_not_executor(qualified_runtime):
    before = plan.toolchain_fingerprint()
    site = _site(qualified_runtime)
    (site / "skcapstone").mkdir(exist_ok=True)
    (site / "skcapstone/unrelated.py").write_text("application change\n")
    assert plan.toolchain_fingerprint() == before
    (site / "pytest/__init__.py").write_text("# changed test runner\n")
    assert plan.toolchain_fingerprint() != before


@pytest.fixture
def qualified(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "fleet").mkdir(parents=True)
    prefix = tmp_path / "python"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python").write_bytes(b"qualified interpreter")
    monkeypatch.setattr(plan, "PREFIX", prefix)
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "a" * 64)
    monkeypatch.setattr(plan, "toolchain_fingerprint", lambda: "e" * 64)
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
    core = {
        "id": "abcd1234",
        "meta": {"repository": "https://example.org/public.git"},
        "acceptance_criteria": ["Run both parser regression cases."],
    }
    recipe = {
        "pytest": {"tests/fleet/test_parser.py": 2},
        "compile": [],
        "lint": [],
        "changelog": False,
    }
    path = profile.qualify_profile(
        home, core, policy, recipe, "operator", "b" * 64, source_sha256="f" * 64
    )
    return home, core, policy, recipe, path


def test_exact_qualified_contract_not_trial_specific(qualified):
    home, core, policy, recipe, _ = qualified
    result = profile.preflight(home, core, ["source-only"], policy)
    checks = profile.recipe_checks(result["recipe"])
    assert result["card"] == "abcd1234"
    assert checks[0]["argv"][-1] == "tests/fleet/test_parser.py"
    assert len(checks) == 1


@pytest.mark.parametrize("change", ["card", "repository", "criteria", "empty", "runtime"])
def test_wrong_or_changed_contract_is_withheld(qualified, monkeypatch, change):
    home, core, policy, _, _ = qualified
    core = copy.deepcopy(core)
    if change == "card":
        core["id"] = "abcd1235"
    elif change == "repository":
        core["meta"]["repository"] = "https://example.org/other.git"
    elif change in ("criteria", "empty"):
        core["acceptance_criteria"] = ["Other requirement"] if change == "criteria" else []
    else:
        monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "c" * 64)
    with pytest.raises((plan.TestEvidenceError, FileNotFoundError)):
        profile.preflight(home, core, ["source-only"], policy)


def test_hosted_and_review_lanes_do_not_require_new_profile(qualified):
    home, _, policy, _, _ = qualified
    assert profile.preflight(home, {}, [], policy) is None
    assert profile.preflight(home, {}, ["source-only", "review"], policy) is None


def test_unrelated_policy_changes_do_not_stale_profile(qualified):
    home, core, policy, _, _ = qualified
    changed = copy.deepcopy(policy)
    changed["remote_review"] = {"enabled": True, "card_ids": ["deadbeef"]}
    assert profile.preflight(home, core, ["source-only"], changed)
    changed["node_quotas"] = {
        changed["authority_host"]: {
            "cpu_quota_percent": 200,
            "memory_max_bytes": 2 * 1024**3,
            "tasks_max": 256,
            "runtime_max_seconds": 3600,
        }
    }
    with pytest.raises(profile.ProfileRequalificationRequired):
        profile.preflight(home, core, ["source-only"], changed)


def test_only_execution_fingerprint_drift_is_eligible_for_native_refresh(qualified, monkeypatch):
    home, core, policy, _, _ = qualified
    value, _ = profile.read_profile(home, core["id"])
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "c" * 64)
    assert profile.fingerprint_only_stale(value, profile.contract(core), policy)
    monkeypatch.setattr(plan, "toolchain_fingerprint", lambda: "f" * 64)
    assert not profile.fingerprint_only_stale(value, profile.contract(core), policy)


def test_source_drift_is_not_eligible_for_automatic_requalification(qualified, monkeypatch):
    home, core, policy, _, _ = qualified
    value, _ = profile.read_profile(home, core["id"])
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "c" * 64)
    assert not profile.fingerprint_only_stale(
        value, profile.contract(core), policy, source_sha256="0" * 64
    )


def test_legacy_profile_without_source_binding_requires_full_qualification(qualified):
    home, core, policy, _, path = qualified
    value = json.loads(path.read_text())
    value.pop("toolchain_sha256")
    value.pop("source_sha256")
    value["schema"] = profile.SCHEMA_V1
    path.write_text(json.dumps(value))
    with pytest.raises(profile.ProfileRequalificationRequired, match="full native qualification"):
        profile.preflight(home, core, ["source-only"], policy)
    assert not profile.fingerprint_only_stale(value, profile.contract(core), policy)


def test_hyphenated_targets_preserve_fixed_argv():
    """Accept literal relative paths without changing the approved commands."""
    recipe = {
        "pytest": {"tests/fleet-checks/test_parser-case.py": 1},
        "compile": ["scripts/fleet/skfleet-working.py"],
        "lint": ["src/package-name/parser-check.py"],
        "changelog": True,
    }
    python = str(plan.PREFIX / "bin/python")
    assert profile.recipe_checks(recipe) == [
        {
            "id": "pytest",
            "argv": [
                python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "production_pytest_selection",
                "-m",
                "not host_systemd",
                "--junitxml=/output/pytest.xml",
                "tests/fleet-checks/test_parser-case.py",
            ],
        },
        {
            "id": "compile",
            "argv": [python, "-m", "py_compile", "scripts/fleet/skfleet-working.py"],
        },
        {
            "id": "lint",
            "argv": [python, "-m", "ruff", "check", "src/package-name/parser-check.py"],
        },
        {"id": "changelog", "argv": [python, "scripts/changelog_fragments.py", "--check"]},
    ]


@pytest.mark.parametrize(
    "bad", ["../tests/a.py", "-p", "tests/a.py::test_a", "tests/a.py;curl", "/tmp/test_a.py"]
)
def test_no_shell_options_or_model_command_syntax(qualified, bad):
    recipe = copy.deepcopy(qualified[3])
    recipe["pytest"] = {bad: 1}
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe)


@pytest.mark.parametrize("category", ["pytest", "compile", "lint"])
@pytest.mark.parametrize(
    "bad",
    [
        "tests/../test-case.py",
        "tests/sub/../../test-case.py",
        "other/test-case.py",
        "/tests/test-case.py",
        "--test-case.py",
        "tests//test-case.py",
        "tests/test-case.py;id",
        "tests/$(id).py",
        "tests/`id`.py",
        "tests/test case.py",
        "tests/test-case.py\n",
        "tests/test-case*.py",
        "tests/test-case.py::test_one",
        "tests/" + "a" * 232 + ".py",
        None,
        17,
    ],
)
def test_hyphen_support_keeps_unsafe_targets_refused(qualified, category, bad):
    """Every target category retains the same path and type boundaries."""
    recipe = copy.deepcopy(qualified[3])
    recipe[category] = {bad: 1} if category == "pytest" else [bad]
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe)


@pytest.mark.parametrize("category", ["pytest", "compile", "lint"])
def test_target_count_and_length_remain_bounded(qualified, category):
    """Accept the existing limits and refuse one additional target or byte."""
    recipe = copy.deepcopy(qualified[3])
    paths = [f"tests/test-case-{index}.py" for index in range(63)]
    paths.append("tests/" + "a" * 231 + ".py")
    assert len(paths[-1]) == 240
    recipe[category] = dict.fromkeys(paths, 1) if category == "pytest" else paths
    profile.recipe_checks(recipe)
    paths.append("tests/test-extra.py")
    recipe[category] = dict.fromkeys(paths, 1) if category == "pytest" else paths
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe)


@pytest.mark.parametrize("category", ["compile", "lint"])
def test_duplicate_hyphenated_targets_are_refused(qualified, category):
    """A valid path does not bypass the unique-target contract."""
    recipe = copy.deepcopy(qualified[3])
    recipe[category] = ["scripts/fleet/skfleet-working.py"] * 2
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe)


@pytest.mark.parametrize("bad", [True, 0, 100001, "1", None])
def test_hyphenated_test_targets_require_bounded_integer_coverage(qualified, bad):
    """Coverage remains a positive bounded integer, never a coercion."""
    recipe = copy.deepcopy(qualified[3])
    recipe["pytest"] = {"tests/test-case.py": bad}
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe)


def test_pytest_still_requires_the_tests_root(qualified):
    """Compile and lint roots cannot become pytest targets."""
    recipe = copy.deepcopy(qualified[3])
    for root in ("scripts", "src"):
        recipe["pytest"] = {f"{root}/test-case.py": 1}
        with pytest.raises(plan.TestEvidenceError, match="explicit test files"):
            profile.recipe_checks(recipe)


def test_arbitrary_argv_and_empty_coverage_rejected(qualified):
    recipe = copy.deepcopy(qualified[3])
    recipe["argv"] = ["sh", "-c", "anything"]
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe)
    recipe.pop("argv")
    recipe["pytest"] = {}
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe)


def test_junit_requires_each_qualified_file_without_failures_or_skips(qualified):
    _, _, _, recipe, path = qualified
    value = json.loads(path.read_text())
    root = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(
        root, "testsuite", tests="2", failures="0", errors="0", skipped="0"
    )
    for index in range(2):
        ElementTree.SubElement(
            suite, "testcase", classname="tests.fleet.test_parser", name=f"test_{index}"
        )
    raw = ElementTree.tostring(root)
    assert plan.junit_counts(raw, value)["total"] == 2
    value["recipe"]["pytest"]["tests/fleet/test_other.py"] = 1
    with pytest.raises(plan.TestEvidenceError, match="coverage"):
        plan.junit_counts(raw, value)
    value["recipe"]["pytest"].pop("tests/fleet/test_other.py")
    ElementTree.SubElement(suite[0], "skipped")
    with pytest.raises(plan.TestEvidenceError, match="skipped"):
        plan.junit_counts(ElementTree.tostring(root), value)


def test_auto_seals_clean_exact_candidate_and_refuses_profile_drift(
    setup,  # noqa: F811
    monkeypatch,  # noqa: F811
):  # noqa:F811
    s = setup
    s.plan_path.unlink()  # Fixture-only removal of the legacy one-candidate plan.
    core = {
        "id": s.binding["source_card"],
        "meta": {"repository": "https://example.org/public.git"},
        "acceptance_criteria": ["Run the parser checks."],
    }
    binding = dict(s.binding, criteria_sha256=profile.contract(core)["criteria_sha256"])
    recipe = {
        "pytest": {"tests/test_parser-case.py": 1},
        "compile": ["scripts/fleet/skfleet-working.py"],
        "lint": ["src/parser-case.py"],
        "changelog": False,
    }
    path = profile.qualify_profile(
        s.home,
        core,
        s.policy,
        recipe,
        "operator",
        "b" * 64,
        source_sha256=plan.source_fingerprint(
            core["meta"]["repository"], s.binding["source_head"], s.binding["source_tree"]
        ),
    )
    profile.seal_candidate(
        s.home, binding, s.workspace, s.policy, "https://example.org/public.git"
    )
    sealed, plan_path, before = plan.load_plan(s.home, binding)
    assert sealed["binding"] == binding
    assert sealed["checks"] == profile.recipe_checks(recipe)
    profile.seal_candidate(
        s.home, binding, s.workspace, s.policy, "https://example.org/public.git"
    )
    assert plan.sha(plan_path.read_bytes()) == before
    value = json.loads(path.read_text())
    value["recipe"]["pytest"]["tests/test_parser-case.py"] = 2
    path.write_text(json.dumps(value))
    with pytest.raises(plan.TestEvidenceError, match="profile changed"):
        plan.load_plan(s.home, binding)


@pytest.mark.parametrize("changed", [False, True])
def test_actual_local_preclaim_block_reads_current_contract(qualified, changed):
    home, core, policy, _, _ = qualified
    script = Path(__file__).parents[2] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(script.read_text())
    block = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "PRODUCTION_POLICY" in ast.unparse(node.test)
        and any(isinstance(n, ast.Name) and n.id == "_test_current" for n in ast.walk(node))
    )
    current = copy.deepcopy(core)
    if changed:
        current["acceptance_criteria"] = ["Amended while materializing workspace."]
    calls = []
    namespace = {
        "PRODUCTION_POLICY": policy,
        "Path": Path,
        "HOME": str(home.parent),
        "cid": core["id"],
        "HOST": "control",
        "d": None,
        "fresh_claimability": {"core": core, "labels": ["source-only"]},
        "_governed_review_metadata": lambda *args: None,
        "authoritative_claimability": lambda *args, **kw: {
            "core": current,
            "labels": ["source-only"],
        },
        "test_preflight": lambda ignored, value, labels, config, **kw: profile.preflight(
            home, value, labels, config, **kw
        ),
        "log": lambda *args: calls.append("withheld"),
        "calls": calls,
    }
    loop = ast.For(
        target=ast.Name(id="item", ctx=ast.Store()),
        iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()),
        body=[block, ast.parse("calls.append('claim')").body[0]],
        orelse=[],
    )
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
            str(script),
            "exec",
        ),
        namespace,
    )
    assert calls == (["withheld"] if changed else ["claim"])


def test_acceptance_hook_seals_before_tests_and_does_not_self_complete(
    setup,  # noqa: F811
    monkeypatch,  # noqa: F811
):  # noqa:F811
    from skcapstone.fleet import production_acceptance as acceptance
    from skcapstone.fleet import production_tests

    s = setup
    s.plan_path.unlink()
    core = {
        "id": s.binding["source_card"],
        "meta": {"repository": "https://example.org/a.git"},
        "acceptance_criteria": ["Verify source regression."],
    }
    binding = dict(s.binding, criteria_sha256=profile.contract(core)["criteria_sha256"])
    profile.qualify_profile(
        s.home,
        core,
        s.policy,
        {"pytest": {"tests/test_any.py": 1}, "compile": [], "lint": [], "changelog": False},
        "operator",
        "b" * 64,
        source_sha256=plan.source_fingerprint(
            core["meta"]["repository"], s.binding["source_head"], s.binding["source_tree"]
        ),
    )
    review, claim = "abcd9876", "d" * 32
    exits = s.home / "evidence/production-review-exits"
    exits.mkdir(parents=True)
    (exits / f"{review}-{claim}.json").write_text("{}")
    directory = acceptance.review_directory(s.home, review, claim)
    directory.mkdir(parents=True)
    (directory / "context.json").write_text("{}")
    context = {
        "policy_sha256": acceptance.digest(s.policy),
        "test_binding": binding,
        "source_workspace": str(s.workspace),
        "source": {
            "card": core["id"],
            "revision": "source",
            "owner": "p",
            "claim": "pclaim",
            "repository": core["meta"]["repository"],
        },
        "review": {"card": review, "revision": "review", "owner": "r", "claim": claim},
    }
    monkeypatch.setattr(acceptance, "read_json", lambda path: context)
    monkeypatch.setattr(acceptance, "terminal_guard", lambda *args: None)
    monkeypatch.setattr(acceptance, "artifacts", lambda *args: None)
    monkeypatch.setattr(
        acceptance,
        "native_state",
        lambda home, card: {
            "revision": "source" if card == core["id"] else "review",
            "owner": "p" if card == core["id"] else "r",
            "claim_revision": "pclaim" if card == core["id"] else claim,
        },
    )
    observed = []

    def tests(home, actual_binding, workspace, policy):
        value, _, _ = plan.load_plan(home, actual_binding)
        observed.append(value["checks"][0]["argv"][-1])
        return None

    monkeypatch.setattr(production_tests, "run_or_read_tests", tests)
    monkeypatch.setattr(
        acceptance, "finish_pair", lambda *args, **kw: pytest.fail("no test proof")
    )
    result = acceptance.reconcile(s.home, s.policy, process_check=lambda *args: None)
    assert result == [{"card": review, "state": "awaiting-trusted-tests"}]
    assert observed == ["tests/test_any.py"]
