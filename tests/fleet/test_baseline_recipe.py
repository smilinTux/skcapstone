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
    many = ["tests/test_a.py::test_%04d_%s" % (n, "p" * 80) for n in range(3000)]
    with pytest.raises(br.BaselineError, match="bound"):
        br.baseline_recipe(many, workspace)


def test_only_a_completed_failing_run_is_curated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        br.plan, "load_plan", lambda *a, **k: ({"profile": {"recipe": br.FULL_SUITE}}, None, "abc")
    )
    run = tmp_path / "fleet/test-runs/abc"
    run.mkdir(parents=True)
    for directory in (run, run.parent, run.parent.parent):
        directory.chmod(0o700)
    (run / "receipt.json").write_text(json.dumps({"checks": [{"id": "pytest", "exit_code": 2}]}))
    (run / "receipt.json").chmod(0o600)
    job = {"binding": {}, "workspace": "x"}
    with pytest.raises(br.BaselineError, match="did not run to completion"):
        br._failed_run(tmp_path, job)


def test_a_curated_recipe_is_extended_with_new_base_failures(workspace):
    current = json.loads(br.baseline_recipe(["tests/test_a.py::test_bad"], workspace, ["tests"]))
    raw = junit(
        ("tests.test_a", "test_ok", ""),
        ("tests.fleet.test_b.TestC", "test_x", '<failure message="drift"/>'),
    )
    extended = json.loads(br.extended_recipe(current, raw, workspace))
    assert extended["deselect"] == [
        "tests/fleet/test_b.py::TestC::test_x",
        "tests/test_a.py::test_bad",
    ]
    assert extended["pytest"] == current["pytest"]
    with pytest.raises(br.BaselineError, match="no new failing"):
        br.extended_recipe(extended, raw, workspace)
    with pytest.raises(br.BaselineError, match="only a curated"):
        br.extended_recipe({"pytest_all": True}, raw, workspace)


def test_targets_compact_clean_directories_and_omit_module_skipped_files():
    passing = {
        "tests/fleet/test_a.py": 3,
        "tests/fleet/test_b.py": 1,
        "tests/test_top.py": 2,
        "tests/test_partly.py": 1,
        "tests/integration/test_ok.py": 1,
    }
    untargetable = {"tests/test_skipmod.py", "tests/integration/test_e2e.py"}
    targets = br.compact_targets(passing, untargetable)
    # tests/ holds a module-level skip, so it splits; tests/fleet is clean.
    assert targets == [
        "tests/test_partly.py",
        "tests/test_top.py",
        "tests/fleet",
        "tests/integration/test_ok.py",
    ]


def test_module_level_skip_in_junit_is_untargetable_not_a_refusal(workspace):
    raw = junit(
        ("tests.test_a", "test_ok", ""),
        ("", "tests.test_a", '<skipped message="collection skipped"/>'),
    )
    passing, baseline, untargetable = br.analyze_junit(raw, workspace)
    assert untargetable == {"tests/test_a.py"} and baseline == []
    with pytest.raises(br.BaselineError):
        br.baseline_from_junit(raw, workspace)


@pytest.mark.parametrize("artifact", ["directory", "receipt", "junit"])
def test_unsynced_run_is_pending(tmp_path, monkeypatch, workspace, artifact):
    monkeypatch.setattr(
        br.plan, "load_plan", lambda *a, **k: ({"profile": {"recipe": br.FULL_SUITE}}, None, "abc")
    )
    run = tmp_path / "fleet/test-runs/abc"
    if artifact != "directory":
        run.mkdir(parents=True)
        run.chmod(0o700)
    if artifact == "junit":
        br.plan.write_once(run / "receipt.json", {"checks": [{"id": "pytest", "exit_code": 1}]})
    with pytest.raises(br.BaselinePendingError, match="awaiting failed-run"):
        br._failed_run(tmp_path, {"binding": {}, "workspace": str(workspace)})


@pytest.mark.parametrize("checks", [["pytest"], ["pytest", "compile", "lint"]])
def test_successful_tests_are_not_baseline_failures(tmp_path, monkeypatch, checks):
    monkeypatch.setattr(
        br.plan, "load_plan", lambda *a, **k: ({"profile": {"recipe": br.FULL_SUITE}}, None, "abc")
    )
    run = tmp_path / "fleet/test-runs/abc"
    run.mkdir(parents=True)
    run.chmod(0o700)
    br.plan.write_once(
        run / "receipt.json",
        {
            "checks": [{"id": name, "exit_code": 0} for name in checks],
            "failure": {"message": "operator test plan is invalid or stale"},
        },
    )
    with pytest.raises(br.BaselinePendingError, match="checks exited zero"):
        br._failed_run(tmp_path, {"binding": {}})


@pytest.mark.parametrize(
    "failure",
    [
        br.BaselinePendingError("awaiting failed-run artifacts"),
        br.BaselineError("invalid evidence"),
    ],
)
def test_curator_reports_skip_separately_and_never_writes(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace

    from skcoord.card_store import CardStore

    root = tmp_path / "fleet/profile-requalifications"
    root.mkdir(parents=True)
    root.chmod(0o700)
    br.plan.write_once(root / "a.job.json", {"card": "test-card"})
    br.plan.write_once(root / "a.job.failed.json", {"card": "test-card"})
    monkeypatch.setattr(CardStore, "fold", lambda *a: SimpleNamespace(archived=False, links={}))

    def unavailable(*args):
        raise failure

    def forbidden(*args, **kwargs):
        pytest.fail("unusable evidence must not write a card")

    monkeypatch.setattr(br, "_failed_run", unavailable)
    result = br.curate_failed_qualifications(tmp_path, run=forbidden)
    assert result == [
        {
            "card": "test-card",
            "state": "skipped" if isinstance(failure, br.BaselinePendingError) else "refused",
            "reason": str(failure),
        }
    ]


@pytest.mark.parametrize("exit_code", [-9, 2, 3, 4, 5, 124, 137])
def test_interruption_crash_and_collection_still_refuse(tmp_path, monkeypatch, exit_code):
    monkeypatch.setattr(
        br.plan, "load_plan", lambda *a, **k: ({"profile": {"recipe": br.FULL_SUITE}}, None, "abc")
    )
    run = tmp_path / "fleet/test-runs/abc"
    run.mkdir(parents=True)
    run.chmod(0o700)
    br.plan.write_once(
        run / "receipt.json", {"checks": [{"id": "pytest", "exit_code": exit_code}]}
    )
    with pytest.raises(br.BaselineError, match="did not run to completion"):
        br._failed_run(tmp_path, {"binding": {}})
