"""Real pytest subtests retain strict parent coverage and failure refusal."""

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet.production_pytest_recipe import recipe_checks


@pytest.mark.parametrize("outcome", ["pass", "fail", "skip"])
def test_real_subtest_junit_matches_parent_coverage(tmp_path, outcome):
    pytest.importorskip("_pytest.subtests")
    tests = tmp_path / "tests"
    tests.mkdir()
    action = {
        "pass": "self.assertEqual(i, i)",
        "fail": "self.assertEqual(i, 0)",
        "skip": "self.skipTest('not qualified')",
    }[outcome]
    (tests / "test_subtests.py").write_text(
        "import unittest\nclass Example(unittest.TestCase):\n"
        "    def test_cases(self):\n"
        "        for i in range(2):\n"
        "            with self.subTest(i=i):\n"
        f"                {action}\n"
    )
    approved = dict(pytest={"tests": 1}, compile=[], lint=[], changelog=False)
    argv = recipe_checks(approved)[0]["argv"]
    argv[0] = sys.executable
    for before, after in {
        "--junitxml=/output/pytest.xml": "--junitxml=" + str(tmp_path / "pytest.xml"),
        "--rootdir=/work": "--rootdir=" + str(tmp_path),
        "--confcutdir=/work": "--confcutdir=" + str(tmp_path),
    }.items():
        argv[argv.index(before)] = after
    env = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
    env.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_PLUGINS="",
        PYTEST_ADDOPTS="",
        PYTHONPATH=str(Path(plan.__file__).parent),
        SKFLEET_SELECTION_OUTPUT=str(tmp_path / "selection.json"),
    )
    result = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, timeout=20)
    raw = (tmp_path / "pytest.xml").read_bytes()
    selection = json.loads((tmp_path / "selection.json").read_text())
    if outcome != "pass":
        with pytest.raises(plan.TestEvidenceError):
            plan.junit_counts(raw, {"recipe": approved}, selection=selection)
        return
    assert result.returncode == 0, result.stdout + result.stderr
    counts = plan.junit_counts(raw, {"recipe": approved}, selection=selection)
    assert counts["total"] == 1
    root = ElementTree.fromstring(raw)
    assert root.find("testsuite").get("tests") == "1"
    properties = {p.get("name"): p.get("value") for p in root.iter("property")}
    assert properties["skfleet_passing_subtests"] == "2"


def test_literal_file_recipe_also_loads_trusted_junit_hook(tmp_path):
    from skcapstone.fleet.production_test_worker import sandbox_command

    approved = dict(pytest={"tests/test_one.py": 1}, compile=[], lint=[], changelog=False)
    argv = recipe_checks(approved)[0]["argv"]
    assert "production_pytest_selection" in argv
    command = sandbox_command(tmp_path, tmp_path / "output", argv)
    at = command.index("/qualified/production_pytest_selection.py")
    assert command[at - 2] == "--ro-bind"
