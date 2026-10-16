"""The declared test recipe contract and the recipe fingerprints recorded as provenance."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import test_recipe
from skcapstone.fleet.production_pytest_recipe import recipe_checks

BASE_RECIPE = {
    "pytest": {"tests/fleet/test_parser.py": 2},
    "compile": [],
    "lint": [],
    "changelog": False,
}


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

    assert test_recipe.initial_recipe(card, tmp_path) == ({"pytest_all": True}, None)
    assert "not host_systemd" in recipe_checks({"pytest_all": True})[0]["argv"]


def test_full_pytest_recipe_handles_sklegal_testpaths_in_isolation(tmp_path):
    from skcapstone.fleet.production_pytest_recipe import recipe_checks

    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "packages/strategy/tests"]\n'
    )
    root_test = tmp_path / "tests/features/matter_activity"
    root_test.mkdir(parents=True)
    (root_test / "test_service.py").write_text("def test_root(): pass\n")
    strategy_tests = tmp_path / "packages/strategy/tests"
    strategy_tests.mkdir(parents=True)
    (strategy_tests / "test_service.py").write_text("def test_strategy(): pass\n")
    integration = tmp_path / "tests/integration"
    integration.mkdir()
    (integration / "test_foundation_contract.py").write_text(
        "raise RuntimeError('host Docker inspection ran during collection')\n"
    )

    argv = recipe_checks({"pytest_all": True})[0]["argv"]
    argv[0] = sys.executable
    argv[argv.index("--junitxml=/output/pytest.xml")] = "--collect-only"
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, check=True)

    assert "tests/features/matter_activity/test_service.py::test_root" in result.stdout
    assert "packages/strategy/tests/test_service.py::test_strategy" in result.stdout
    assert "test_foundation_contract" not in result.stdout


def test_initial_recipe_rejects_ambiguous_language_scope():
    with pytest.raises(plan.TestEvidenceError, match="mixed test scope"):
        test_recipe.initial_recipe(
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

    recipe, environment = test_recipe.initial_recipe(
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

    recipe, environment = test_recipe.initial_recipe(
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

    selected, environment = test_recipe.initial_recipe(core, tmp_path)

    assert selected == recipe
    assert environment is None
    validate_source(selected, tmp_path)


def test_hyphenated_targets_preserve_fixed_argv():
    """Accept literal relative paths without changing the approved commands."""
    recipe = {
        "pytest": {"tests/fleet-checks/test_parser-case.py": 1},
        "compile": ["scripts/fleet/skfleet-working.py"],
        "lint": ["src/package-name/parser-check.py"],
        "changelog": True,
    }
    python = str(plan.PREFIX / "bin/python")
    assert recipe_checks(recipe) == [
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
                "--import-mode=importlib",
                "--ignore=tests/integration/test_foundation_contract.py",
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
def test_no_shell_options_or_model_command_syntax(bad):
    recipe = copy.deepcopy(BASE_RECIPE)
    recipe["pytest"] = {bad: 1}
    with pytest.raises(plan.TestEvidenceError):
        recipe_checks(recipe)


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
def test_hyphen_support_keeps_unsafe_targets_refused(category, bad):
    """Every target category retains the same path and type boundaries."""
    recipe = copy.deepcopy(BASE_RECIPE)
    recipe[category] = {bad: 1} if category == "pytest" else [bad]
    with pytest.raises(plan.TestEvidenceError):
        recipe_checks(recipe)


@pytest.mark.parametrize("category", ["pytest", "compile", "lint"])
def test_target_count_and_length_remain_bounded(category):
    """Accept the existing limits and refuse one additional target or byte."""
    recipe = copy.deepcopy(BASE_RECIPE)
    paths = [f"tests/test-case-{index}.py" for index in range(63)]
    paths.append("tests/" + "a" * 231 + ".py")
    assert len(paths[-1]) == 240
    recipe[category] = dict.fromkeys(paths, 1) if category == "pytest" else paths
    recipe_checks(recipe)
    paths.append("tests/test-extra.py")
    recipe[category] = dict.fromkeys(paths, 1) if category == "pytest" else paths
    with pytest.raises(plan.TestEvidenceError):
        recipe_checks(recipe)


@pytest.mark.parametrize("category", ["compile", "lint"])
def test_duplicate_hyphenated_targets_are_refused(category):
    """A valid path does not bypass the unique-target contract."""
    recipe = copy.deepcopy(BASE_RECIPE)
    recipe[category] = ["scripts/fleet/skfleet-working.py"] * 2
    with pytest.raises(plan.TestEvidenceError):
        recipe_checks(recipe)


@pytest.mark.parametrize("bad", [True, 0, 100001, "1", None])
def test_hyphenated_test_targets_require_bounded_integer_coverage(bad):
    """Coverage remains a positive bounded integer, never a coercion."""
    recipe = copy.deepcopy(BASE_RECIPE)
    recipe["pytest"] = {"tests/test-case.py": bad}
    with pytest.raises(plan.TestEvidenceError):
        recipe_checks(recipe)


def test_pytest_still_requires_the_tests_root():
    """Compile and lint roots cannot become pytest targets."""
    recipe = copy.deepcopy(BASE_RECIPE)
    for root in ("scripts", "src"):
        recipe["pytest"] = {f"{root}/test-case.py": 1}
        with pytest.raises(plan.TestEvidenceError, match="explicit test files"):
            recipe_checks(recipe)


def test_arbitrary_argv_and_empty_coverage_rejected():
    recipe = copy.deepcopy(BASE_RECIPE)
    recipe["argv"] = ["sh", "-c", "anything"]
    with pytest.raises(plan.TestEvidenceError):
        recipe_checks(recipe)
    recipe.pop("argv")
    recipe["pytest"] = {}
    with pytest.raises(plan.TestEvidenceError):
        recipe_checks(recipe)


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
        test_recipe.initial_recipe(
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
    recipe, _ = test_recipe.initial_recipe(
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
    recipe, _ = test_recipe.initial_recipe(
        {
            "description": "Repair apps/web/src/pages/Workflow.tsx and its tests.",
            "acceptance_criteria": ["Vitest passes."],
            "links": {"repository": "https://github.com/example/sklegal"},
        },
        tmp_path,
    )
    assert recipe == {"vitest": {"src/pages/Workflow.test.tsx": 1}}
    node.checks(recipe)
