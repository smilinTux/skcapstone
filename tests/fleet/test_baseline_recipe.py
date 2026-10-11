"""Baseline recipes come only from exact sealed evidence of a completed failing run."""

import json

import pytest

from skcapstone.fleet import baseline_recipe as br
from skcapstone.fleet.production_pytest_recipe import recipe_checks


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "tests/fleet").mkdir(parents=True)
    (tmp_path / "tests/test_a.py").write_text("def test_ok(): pass\n")
    (tmp_path / "tests/fleet/test_b.py").write_text("class TestC:\n    def test_x(self): pass\n")
    return tmp_path


def junit(*cases):
    body = "".join(
        '<testcase classname="%s" name="%s">%s</testcase>' % (c, n, inner) for c, n, inner in cases
    )
    return ('<testsuites><testsuite name="pytest">%s</testsuite></testsuites>' % body).encode()


def test_failed_errored_skipped_cases_map_to_exact_node_ids(workspace):
    raw = junit(
        ("tests.test_a", "test_ok", ""),
        ("tests.test_a", "test_bad[p1]", '<failure message="x"/>'),
        ("tests.fleet.test_b.TestC", "test_x", '<error message="host"/>'),
        ("tests.fleet.test_b", "test_skip", '<skipped message="s"/>'),
    )
    ids = br.baseline_from_junit(raw, workspace)
    assert ids == [
        "tests/fleet/test_b.py::TestC::test_x",
        "tests/fleet/test_b.py::test_skip",
        "tests/test_a.py::test_bad[p1]",
    ]
    text = br.baseline_recipe(ids, workspace)
    recipe = json.loads(text)
    assert recipe["pytest"] == {"tests": 1} and recipe["deselect"] == ids
    argv = recipe_checks(recipe)[0]["argv"]
    assert "--skfleet-baseline-node=tests/test_a.py::test_bad[p1]" in argv


@pytest.mark.parametrize(
    "case",
    [
        ("", "tests/test_gone.py", '<error message="collection failure"/>'),
        ("tests.test_missing", "test_x", '<failure message="x"/>'),
    ],
)
def test_collection_errors_and_unresolvable_cases_refuse(workspace, case):
    with pytest.raises(br.BaselineError):
        br.baseline_from_junit(junit(case), workspace)


def test_empty_or_oversized_baselines_refuse(workspace):
    with pytest.raises(br.BaselineError, match="no failing"):
        br.baseline_recipe([], workspace)
    many = ["tests/test_a.py::test_%04d_%s" % (n, "p" * 40) for n in range(400)]
    with pytest.raises(br.BaselineError, match="bound"):
        br.baseline_recipe(many, workspace)


def test_only_a_completed_full_suite_failure_is_curated(tmp_path, monkeypatch):
    job = {"initial_profile_value": {"recipe": {"pytest": {"tests/test_a.py": 1}}}}
    with pytest.raises(br.BaselineError, match="not a full-suite"):
        br._failed_run(tmp_path, job)
    monkeypatch.setattr(br.plan, "load_plan", lambda *a, **k: ({}, None, "abc"))
    run = tmp_path / "fleet/test-runs/abc"
    run.mkdir(parents=True)
    for directory in (run, run.parent, run.parent.parent):
        directory.chmod(0o700)
    (run / "receipt.json").write_text(json.dumps({"checks": [{"id": "pytest", "exit_code": 2}]}))
    (run / "receipt.json").chmod(0o600)
    job = {"initial_profile_value": {"recipe": br.FULL_SUITE}, "binding": {}, "workspace": "x"}
    with pytest.raises(br.BaselineError, match="did not run to completion"):
        br._failed_run(tmp_path, job)
