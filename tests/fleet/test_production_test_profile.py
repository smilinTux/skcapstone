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


def test_toolchain_fingerprint_accepts_venv_python_symlink(
    qualified_runtime, tmp_path, monkeypatch
):
    python = qualified_runtime / "bin/python"
    python.unlink()
    base_prefix = tmp_path / "base-python"
    base_prefix.mkdir()
    interpreter = base_prefix / "python3"
    interpreter.write_bytes(b"base interpreter bytes\n")
    python.symlink_to(interpreter)
    monkeypatch.setattr(plan.sys, "base_prefix", str(base_prefix))

    before = plan.toolchain_fingerprint()
    interpreter.write_bytes(b"updated base interpreter bytes\n")

    assert plan.toolchain_fingerprint() != before


def test_toolchain_fingerprint_rejects_python_symlink_outside_base_prefix(
    qualified_runtime, tmp_path, monkeypatch
):
    python = qualified_runtime / "bin/python"
    python.unlink()
    outside = tmp_path / "outside-python"
    outside.write_bytes(b"untrusted interpreter bytes\n")
    python.symlink_to(outside)
    base_prefix = tmp_path / "base-python"
    base_prefix.mkdir()
    monkeypatch.setattr(plan.sys, "base_prefix", str(base_prefix))

    with pytest.raises(plan.TestEvidenceError, match="interpreter escapes its base prefix"):
        plan.toolchain_fingerprint()


def test_full_pytest_recipe_requires_a_clean_nonempty_junit_report():
    root = ElementTree.Element("testsuites", tests="1", failures="0", errors="0", skipped="0")
    suite = ElementTree.SubElement(
        root, "testsuite", tests="1", failures="0", errors="0", skipped="0"
    )
    ElementTree.SubElement(suite, "testcase", classname="test_sample", name="passes")
    raw = ElementTree.tostring(root)
    assert plan.junit_counts(raw, {"recipe": {"pytest_all": True}}) == {
        "total": 1,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
    }
    with pytest.raises(plan.TestEvidenceError, match="full pytest suite failed"):
        failed = ElementTree.Element(
            "testsuites", tests="1", failures="1", errors="0", skipped="0"
        )
        failed_suite = ElementTree.SubElement(
            failed, "testsuite", tests="1", failures="1", errors="0", skipped="0"
        )
        failed_case = ElementTree.SubElement(
            failed_suite, "testcase", classname="test_sample", name="fails"
        )
        ElementTree.SubElement(failed_case, "failure")
        plan.junit_counts(ElementTree.tostring(failed), {"recipe": {"pytest_all": True}})


def test_initial_recipe_uses_fixed_full_pytest_for_python_scope(tmp_path):
    from skcapstone.fleet.production_pytest_recipe import recipe_checks

    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "tests").mkdir()
    card = {
        "description": "Repair src/skcapstone/example.py and test it with pytest.",
        "acceptance_criteria": ["tests must pass"],
    }

    assert profile.initial_recipe(card, tmp_path) == ({"pytest_all": True}, None)
    assert "not host_systemd" in recipe_checks({"pytest_all": True})[0]["argv"]


def test_initial_recipe_rejects_ambiguous_language_scope():
    with pytest.raises(plan.TestEvidenceError, match="mixed test scope"):
        profile.initial_recipe(
            {
                "description": "Change apps/web/src/page.tsx and src/api.py",
                "acceptance_criteria": [],
            },
            Path("/unused"),
        )


def test_initial_recipe_uses_only_the_fixed_vitest_suite(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_node as node

    target = tmp_path / "apps/web/src/pages/Workflow.test.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("export {};\n")
    (tmp_path / "apps/web/package.json").write_text("{}\n")
    monkeypatch.setattr(node, "qualified_environment", lambda _workspace: {"artifact": "a" * 64})

    recipe, environment = profile.initial_recipe(
        {
            "description": "Repair apps/web/src/pages/Workflow.tsx and its tests.",
            "acceptance_criteria": ["Vitest passes."],
            "links": {"repository": "https://github.com/example/sklegal"},
        },
        tmp_path,
    )

    assert recipe == {"vitest": {"src/pages/Workflow.test.tsx": 1}}
    assert environment == {"artifact": "a" * 64}


def test_initial_recipe_builds_composite_for_python_and_frontend_scope(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_node as node

    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "tests").mkdir()
    target = tmp_path / "apps/web/src/pages/Workflow.test.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("export {};\n")
    (tmp_path / "apps/web/package.json").write_text("{}\n")
    monkeypatch.setattr(node, "qualified_environment", lambda _workspace: {"artifact": "a" * 64})

    recipe, environment = profile.initial_recipe(
        {
            "description": "Run the Python checks and full frontend suite.",
            "acceptance_criteria": ["Python and frontend acceptance pass."],
            "links": {"repository": "https://github.com/example/sklegal"},
        },
        tmp_path,
    )

    assert recipe == {"pytest_all": True, "vitest": {"src/pages/Workflow.test.tsx": 1}}
    assert environment == {"artifact": "a" * 64}


def test_initial_recipe_accepts_only_a_validated_operator_recipe(tmp_path):
    from skcapstone.fleet.production_pytest_recipe import validate_source

    target = tmp_path / "tests/test_gateway.py"
    target.parent.mkdir()
    target.write_text("def test_gateway(): pass\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    recipe = {
        "pytest": {"tests/test_gateway.py": 1},
        "compile": [],
        "lint": [],
        "changelog": False,
    }
    core = {
        "description": "Run the exact contract tests.",
        "acceptance_criteria": ["The Python gateway suite passes."],
        "links": {
            "repository": "https://github.com/example/sklegal",
            "test_profile_recipe": json.dumps(recipe, sort_keys=True),
        },
    }

    selected, environment = profile.initial_recipe(core, tmp_path)

    assert selected == recipe
    assert environment is None
    validate_source(selected, tmp_path)


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


def test_composite_profile_uses_composite_schema(qualified, monkeypatch):
    from skcapstone.fleet import production_test_composite as composite
    from skcapstone.fleet import production_test_node as node

    _home, core, policy, _recipe, _path = qualified
    monkeypatch.setattr(node, "validate_environment", lambda *_args, **_kwargs: None)
    value = profile.profile_value(
        core,
        policy,
        {"pytest_all": True, "vitest": {"src/pages/Workflow.test.tsx": 1}},
        "operator",
        "b" * 64,
        source_sha256="f" * 64,
        node_environment={"artifact": "a" * 64},
    )

    assert value["schema"] == composite.SCHEMA
    assert composite.is_composite(value)


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


def test_profile_on_exact_legacy_skenv_toolchain_is_requalified_once(qualified, monkeypatch):
    """Switching to the clean prefix must not strand current-schema profiles."""
    home, core, policy, _, _ = qualified
    value, _ = profile.read_profile(home, core["id"])
    monkeypatch.setattr(plan, "toolchain_fingerprint", lambda: "f" * 64)
    monkeypatch.setattr(plan, "legacy_toolchain_fingerprint", lambda: "e" * 64)
    assert profile.fingerprint_only_stale(value, profile.contract(core), policy)
    with pytest.raises(profile.ProfileRequalificationRequired):
        profile.preflight(home, core, ["source-only"], policy)
    assert not profile.fingerprint_only_stale(
        value, profile.contract(core), policy, source_sha256="0" * 64
    )


@pytest.mark.parametrize("legacy", [None, "d" * 64, "f" * 64])
def test_toolchain_change_without_exact_legacy_match_stays_blocked(qualified, monkeypatch, legacy):
    home, core, policy, _, _ = qualified
    value, _ = profile.read_profile(home, core["id"])
    monkeypatch.setattr(plan, "toolchain_fingerprint", lambda: "f" * 64)
    monkeypatch.setattr(plan, "legacy_toolchain_fingerprint", lambda: legacy)
    assert not profile.fingerprint_only_stale(value, profile.contract(core), policy)
    assert not profile.legacy_full_qualification_required(value, profile.contract(core), policy)
    with pytest.raises(plan.TestEvidenceError, match="execution environment changed"):
        profile.preflight(home, core, ["source-only"], policy)


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
    from skcoord.card_store import CardCore, CardStore

    CardStore(s.home).create(
        CardCore(
            id=review,
            title="[REVIEW][S] Synthetic acceptance",
            created_by="r",
            initial_labels=["review", "source-only", "parent-" + core["id"]],
        )
    )
    exits = s.home / "evidence/production-review-exits"
    exits.mkdir(parents=True)
    (exits / f"{review}-{claim}.json").write_text("{}")
    directory = acceptance.review_directory(s.home, review, claim)
    directory.mkdir(parents=True)
    (directory / "context.json").write_text("{}")
    context = {
        "schema": "skfleet.production-acceptance-context/v2",
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
        "review": {
            "card": review,
            "revision": "review",
            "owner": "r",
            "claim": claim,
            "terminal": {
                "host": "control",
                "model": "qualified-review",
                "lane": "deepseek",
                "unit": "skfleet-worker-deepseek-abcd9876.service",
                "invocation": "f" * 32,
                "source_head": "a" * 40,
            },
            "launch_event": {
                "route_identity": {"policy_sha256": acceptance.digest(s.policy)},
            },
        },
    }
    monkeypatch.setattr(acceptance, "read_json", lambda path: context)
    monkeypatch.setattr(acceptance, "terminal_guard", lambda *args: None)
    monkeypatch.setattr(acceptance, "artifacts", lambda *args: None)
    monkeypatch.setattr(acceptance, "production_receipt_allowed", lambda *args: True)
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


def _both_projects(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "tests").mkdir()
    target = tmp_path / "apps/web/src/pages/Workflow.test.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("export {};\n")
    (tmp_path / "apps/web/package.json").write_text("{}\n")


def test_initial_recipe_without_stated_scope_is_refused(tmp_path):
    """No guessed whole-suite recipe: SKLegal's full suite cannot be green in the sandbox."""
    _both_projects(tmp_path)
    with pytest.raises(plan.TestEvidenceError, match="no supported initial test recipe"):
        profile.initial_recipe(
            {
                "title": "Rebase scoped Product Status onto current main",
                "acceptance_criteria": ["Status page renders the live state."],
                "links": {"repository": "https://github.com/example/sklegal"},
            },
            tmp_path,
        )


def test_initial_recipe_reads_frontend_label_as_node_scope(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_node as node

    target = tmp_path / "apps/web/src/pages/Workflow.test.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("export {};\n")
    (tmp_path / "apps/web/package.json").write_text("{}\n")
    monkeypatch.setattr(node, "qualified_environment", lambda _workspace: {"artifact": "a" * 64})
    recipe, _ = profile.initial_recipe(
        {
            "title": "Wait for the live audit",
            "labels": ["frontend", "source-only"],
            "acceptance_criteria": ["Audit waits for the live state."],
            "links": {"repository": "https://github.com/example/sklegal"},
        },
        tmp_path,
    )
    assert recipe == {"vitest": {"src/pages/Workflow.test.tsx": 1}}


def test_node_recipe_ignores_vitest_snapshot_artifacts(tmp_path, monkeypatch):
    from skcapstone.fleet import production_test_node as node

    target = tmp_path / "apps/web/src/pages/Workflow.test.tsx"
    target.parent.mkdir(parents=True)
    target.write_text("export {};\n")
    snapshot = tmp_path / "apps/web/src/__snapshots__/visual-regression.test.tsx.snap"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("// snapshot\n")
    (tmp_path / "apps/web/package.json").write_text("{}\n")
    monkeypatch.setattr(node, "qualified_environment", lambda _workspace: {"artifact": "a" * 64})
    recipe, _ = profile.initial_recipe(
        {
            "description": "Repair apps/web/src/pages/Workflow.tsx and its tests.",
            "acceptance_criteria": ["Vitest passes."],
            "links": {"repository": "https://github.com/example/sklegal"},
        },
        tmp_path,
    )
    assert recipe == {"vitest": {"src/pages/Workflow.test.tsx": 1}}
    node.checks(recipe)


def test_acceptance_requalifies_only_fingerprint_drift(qualified, monkeypatch):
    home, core, policy, _, _ = qualified
    value, _ = profile.read_profile(home, core["id"])
    expected = profile.contract(core)
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "c" * 64)
    monkeypatch.setattr(plan, "toolchain_fingerprint", lambda: "d" * 64)
    assert profile.acceptance_requalifiable(value, dict(expected, source_sha256="0" * 64), policy)
    for key, changed in (
        ("card", "abcd1235"),
        ("repository", "https://example.org/other.git"),
        ("criteria_sha256", "9" * 64),
    ):
        assert not profile.acceptance_requalifiable(
            value, dict(expected, **{key: changed}), policy
        )
    assert not profile.acceptance_requalifiable(
        value, expected, dict(policy, authority_host="elsewhere")
    )


def _binding(core, head="1" * 40, tree="2" * 40):
    return {
        "source_card": core["id"],
        "source_owner": "pi-glm-builder-node-chiap02-" + core["id"],
        "source_claim_revision": "3" * 32,
        "source_head": head,
        "source_tree": tree,
        "source_revision": "r",
        "criteria_sha256": profile.contract(core)["criteria_sha256"],
    }


@pytest.mark.parametrize("held", [True, False])
def test_accepted_trusted_tests_publish_the_candidate_profile(qualified, monkeypatch, held):
    home, core, policy, _, _ = qualified
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "c" * 64)
    binding = _binding(core)
    calls = []

    class Store:
        def __init__(self, *_args):
            pass

        def fold(self, card_id):
            from types import SimpleNamespace

            return SimpleNamespace(
                owner=binding["source_owner"] if held else None,
                model_dump=lambda mode=None: dict(core),
            )

    monkeypatch.setattr(profile, "CardStore", Store)
    monkeypatch.setattr(profile, "supersede_profile", lambda *a, **k: calls.append(k))
    assert profile.refresh_accepted_profile(
        home, binding, policy, core["meta"]["repository"], "9" * 64
    )
    (kwargs,) = calls
    assert kwargs["source_sha256"] == plan.source_fingerprint(
        core["meta"]["repository"], binding["source_head"], binding["source_tree"]
    )
    assert kwargs["runtime_sha256"] == "c" * 64
    if held:
        assert kwargs["source_claim"] == {
            "owner": binding["source_owner"],
            "claim_revision": binding["source_claim_revision"],
        }
        assert kwargs["unclaimed"] is False
    else:
        assert kwargs["source_claim"] is None and kwargs["unclaimed"] is True


def test_contract_change_is_never_requalified_by_acceptance(qualified, monkeypatch):
    home, core, policy, _, _ = qualified
    binding = dict(_binding(core), criteria_sha256="9" * 64)
    monkeypatch.setattr(profile, "supersede_profile", lambda *a, **k: pytest.fail("published"))
    with pytest.raises(plan.TestEvidenceError):
        profile.refresh_accepted_profile(
            home, binding, policy, core["meta"]["repository"], "9" * 64
        )
