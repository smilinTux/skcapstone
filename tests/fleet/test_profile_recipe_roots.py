"""Safe nested/directory recipes and exact recorded baseline exclusions."""

# ruff: noqa: F811

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profile
from tests.fleet.test_production_tests import setup  # noqa: F401


def recipe(targets=None, **options):
    return {
        "pytest": targets or {"v1/tests": 2},
        "compile": [],
        "lint": [],
        "changelog": False,
        **options,
    }


def test_nested_directory_and_versioned_test_targets_preserve_fixed_commands():
    for target in (
        "v1/tests/test_backup.py",
        "v1/tests",
        "tests",
        "tests/contracts/v1.0.0/test_contract.py",
    ):
        checks = profile.recipe_checks(recipe({target: 1}))
        assert target in checks[0]["argv"]
        assert checks[0]["argv"][:4] == [str(plan.PREFIX / "bin/python"), "-m", "pytest", "-q"]


def test_operator_file_cap_is_bounded_and_legacy_default_unchanged():
    files = {f"tests/test_{n}.py": 1 for n in range(80)}
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe(files))
    assert profile.recipe_checks(recipe(files, file_cap=128))
    for bad in (True, 0, 4097, "128"):
        with pytest.raises(plan.TestEvidenceError):
            profile.recipe_checks(recipe(files, file_cap=bad))


@pytest.mark.parametrize(
    "target",
    (
        "../v1/tests",
        "/v1/tests",
        "v1/../tests",
        "v1//tests",
        "v1/tests/../../bad.py",
        "v1/tests/*",
        "v1/tests;id",
        "src/test_bad.py",
    ),
)
def test_unsafe_nested_targets_remain_rejected(target):
    with pytest.raises(plan.TestEvidenceError):
        profile.recipe_checks(recipe({target: 1}))


def test_recipe_targets_must_exist_inside_real_sealed_source(tmp_path):
    from skcapstone.fleet.production_pytest_recipe import validate_source

    tests = tmp_path / "v1/tests"
    tests.mkdir(parents=True)
    (tests / "test_backup.py").write_text("def test_ok(): pass\n")
    validate_source(recipe(), tmp_path)
    with pytest.raises(plan.TestEvidenceError, match="source"):
        validate_source(recipe({"v1/tests/test_missing.py": 1}), tmp_path)
    (tests / "test_escape.py").symlink_to("/usr/bin/python3")
    with pytest.raises(plan.TestEvidenceError, match="source"):
        validate_source(recipe(), tmp_path)


def test_deselection_is_recorded_as_exact_ids_and_never_prefix_options():
    node = "v1/tests/test_backup.py::test_known_failure"
    checks = profile.recipe_checks(recipe(deselect=[node]))
    assert "--skfleet-baseline-node=" + node in checks[0]["argv"]
    assert not any(arg.startswith("--deselect") for arg in checks[0]["argv"])
    for bad in (
        [node, node],
        ["v1/tests/*.py::test_bad"],
        ["tests/test_other.py::test_bad"],
        ["v1/tests/test_backup.py"],
        ["../test.py::test_bad"],
    ):
        with pytest.raises(plan.TestEvidenceError):
            profile.recipe_checks(recipe(deselect=bad))


def run_fixture(tmp_path, *, extra_failure=False, missing=False):
    tests = tmp_path / "v1/tests"
    tests.mkdir(parents=True)
    (tests / "test_backup.py").write_text(
        "def test_ok(): pass\n"
        "def test_known_failure(): assert False\n"
        f"def test_known_failure_extra(): assert {not extra_failure}\n"
    )
    node = "v1/tests/test_backup.py::test_" + ("missing" if missing else "known_failure")
    approved = recipe(deselect=[node])
    from skcapstone.fleet.production_pytest_recipe import validate_source

    validate_source(approved, tmp_path)
    check = profile.recipe_checks(approved)[0]["argv"]
    argv = [sys.executable, *check[1:]]
    argv[argv.index("--junitxml=/output/pytest.xml")] = "--junitxml=" + str(
        tmp_path / "pytest.xml"
    )
    argv[argv.index("--rootdir=/work")] = "--rootdir=" + str(tmp_path)
    argv[argv.index("--confcutdir=/work")] = "--confcutdir=" + str(tmp_path)
    env = dict(
        os.environ,
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONPATH=str(Path(plan.__file__).parent),
        PYTEST_ADDOPTS="",
        PYTEST_PLUGINS="",
        SKFLEET_SELECTION_OUTPUT=str(tmp_path / "selection.json"),
    )
    result = subprocess.run(
        argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20
    )
    return result, approved, node


def test_real_pytest_exact_baseline_and_directory_coverage(tmp_path):
    result, approved, node = run_fixture(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    selection = json.loads((tmp_path / "selection.json").read_text())
    counts = plan.junit_counts(
        (tmp_path / "pytest.xml").read_bytes(), {"recipe": approved}, selection=selection
    )
    assert counts["total"] == 2 and counts["deselected"] == [node]
    assert selection["deselected"] == [node]
    assert any(n.endswith("::test_known_failure_extra") for n in selection["selected"])
    with pytest.raises(plan.TestEvidenceError):
        plan.junit_counts((tmp_path / "pytest.xml").read_bytes(), {"recipe": approved})
    selection["selected"].pop()
    with pytest.raises(plan.TestEvidenceError):
        plan.junit_counts(
            (tmp_path / "pytest.xml").read_bytes(), {"recipe": approved}, selection=selection
        )


def test_baseline_cannot_hide_a_similarly_named_new_failure(tmp_path):
    result, approved, _ = run_fixture(tmp_path, extra_failure=True)
    assert result.returncode == 1
    with pytest.raises(plan.TestEvidenceError):
        plan.junit_counts(
            (tmp_path / "pytest.xml").read_bytes(),
            {"recipe": approved},
            selection=json.loads((tmp_path / "selection.json").read_text()),
        )


def test_absent_known_failure_is_not_silently_ignored(tmp_path):
    result, _, _ = run_fixture(tmp_path, missing=True)
    assert result.returncode != 0
    assert "baseline node IDs missing" in result.stdout + result.stderr


def test_collection_hook_mount_is_readonly_and_fingerprinted(tmp_path):
    from skcapstone.fleet.production_test_worker import sandbox_command

    approved = recipe()
    command = sandbox_command(
        tmp_path,
        tmp_path / "output",
        profile.recipe_checks(approved)[0]["argv"],
        {"recipe": approved},
    )
    mount = command.index("/qualified/production_pytest_selection.py")
    assert command[mount - 2] == "--ro-bind"
    assert command[mount - 1] == str(plan.HARNESS_ROOT / "production_pytest_selection.py")
    assert "production_pytest_selection.py" in plan.HARNESS_MODULES
    assert "production_pytest_recipe.py" in plan.HARNESS_MODULES
    assert "--unshare-all" in command and "--clearenv" in command
    assert command[command.index("PYTHONPATH") + 1].endswith(":/qualified")


def test_candidate_sealing_rejects_missing_qualified_source_target(setup):
    """A valid qualified profile does not excuse missing candidate source."""
    core = {
        "id": "89508f84",
        "meta": {"repository": "https://example.org/public.git"},
        "acceptance_criteria": ["Run the nested parser tests."],
    }
    binding = dict(
        setup.binding,
        source_card=core["id"],
        criteria_sha256=profile.contract(core)["criteria_sha256"],
    )
    approved = recipe({"v1/tests/test_missing.py": 1})
    path = profile.qualify_profile(setup.home, core, setup.policy, approved, "operator", "b" * 64)
    qualified_profile, _ = profile.read_profile(setup.home, core["id"])
    with pytest.raises(plan.TestEvidenceError, match="source"):
        plan.seal_plan(
            setup.home,
            binding,
            setup.workspace,
            setup.policy,
            "operator",
            "b" * 64,
            profile=qualified_profile,
        )
    assert path.exists()
    assert not (
        setup.home / "fleet/test-plans" / f"{core['id']}-{binding['source_head']}.json"
    ).exists()
