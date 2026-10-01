"""Qualified contracts gate tokens and seal exact candidates without model commands."""

import ast
import copy
import json
import socket
from pathlib import Path
from xml.etree import ElementTree

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profile
from tests.fleet.test_production_tests import setup  # noqa: F401


@pytest.fixture
def qualified(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "fleet").mkdir(parents=True)
    prefix = tmp_path / "python"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/python").write_bytes(b"qualified interpreter")
    monkeypatch.setattr(plan, "PREFIX", prefix)
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "a" * 64)
    policy = {"authority_host": socket.gethostname().split(".")[0].lower()}
    core = {"id": "abcd1234", "meta": {"repository": "https://example.org/public.git"},
            "acceptance_criteria": ["Run both parser regression cases."]}
    recipe = {"pytest": {"tests/fleet/test_parser.py": 2}, "compile": [],
              "lint": [], "changelog": False}
    path = profile.qualify_profile(home, core, policy, recipe, "operator", "b" * 64)
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


@pytest.mark.parametrize("bad", ["../tests/a.py", "-p", "tests/a.py::test_a",
                                  "tests/a.py;curl", "/tmp/test_a.py"])
def test_no_shell_options_or_model_command_syntax(qualified, bad):
    recipe = copy.deepcopy(qualified[3])
    recipe["pytest"] = {bad: 1}
    with pytest.raises(plan.TestEvidenceError):
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
    suite = ElementTree.SubElement(root, "testsuite", tests="2", failures="0",
                                   errors="0", skipped="0")
    for index in range(2):
        ElementTree.SubElement(suite, "testcase", classname="tests.fleet.test_parser",
                               name=f"test_{index}")
    raw = ElementTree.tostring(root)
    assert plan.junit_counts(raw, value)["total"] == 2
    value["recipe"]["pytest"]["tests/fleet/test_other.py"] = 1
    with pytest.raises(plan.TestEvidenceError, match="coverage"):
        plan.junit_counts(raw, value)
    value["recipe"]["pytest"].pop("tests/fleet/test_other.py")
    ElementTree.SubElement(suite[0], "skipped")
    with pytest.raises(plan.TestEvidenceError, match="skipped"):
        plan.junit_counts(ElementTree.tostring(root), value)


def test_auto_seals_clean_exact_candidate_and_refuses_profile_drift(setup, monkeypatch):  # noqa:F811
    s = setup
    s.plan_path.unlink()  # Fixture-only removal of the legacy one-candidate plan.
    core = {"id": s.binding["source_card"],
            "meta": {"repository": "https://example.org/public.git"},
            "acceptance_criteria": ["Run the parser checks."]}
    binding = dict(s.binding, criteria_sha256=profile.contract(core)["criteria_sha256"])
    recipe = {"pytest": {"tests/test_parser.py": 1}, "compile": [],
              "lint": [], "changelog": False}
    path = profile.qualify_profile(s.home, core, s.policy, recipe, "operator", "b" * 64)
    profile.seal_candidate(s.home, binding, s.workspace, s.policy,
                           "https://example.org/public.git")
    sealed, plan_path, before = plan.load_plan(s.home, binding)
    assert sealed["binding"] == binding
    assert sealed["checks"][0]["argv"][-1] == "tests/test_parser.py"
    profile.seal_candidate(s.home, binding, s.workspace, s.policy,
                           "https://example.org/public.git")
    assert plan.sha(plan_path.read_bytes()) == before
    value = json.loads(path.read_text())
    value["recipe"]["pytest"]["tests/test_parser.py"] = 2
    path.write_text(json.dumps(value))
    with pytest.raises(plan.TestEvidenceError, match="profile changed"):
        plan.load_plan(s.home, binding)


@pytest.mark.parametrize("changed", [False, True])
def test_actual_local_preclaim_block_reads_current_contract(qualified, changed):
    home, core, policy, _, _ = qualified
    script = Path(__file__).parents[2] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(script.read_text())
    block = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.If) and "PRODUCTION_POLICY" in ast.unparse(node.test)
                 and any(isinstance(n, ast.Name) and n.id == "_test_current"
                         for n in ast.walk(node)))
    current = copy.deepcopy(core)
    if changed:
        current["acceptance_criteria"] = ["Amended while materializing workspace."]
    calls = []
    namespace = {
        "PRODUCTION_POLICY": policy, "Path": Path, "HOME": str(home.parent),
        "cid": core["id"], "HOST": "control", "d": None,
        "fresh_claimability": {"core": core, "labels": ["source-only"]},
        "_governed_review_metadata": lambda *args: None,
        "authoritative_claimability": lambda *args, **kw: {
            "core": current, "labels": ["source-only"]},
        "test_preflight": lambda ignored, value, labels, config:
            profile.preflight(home, value, labels, config),
        "log": lambda *args: calls.append("withheld"), "calls": calls,
    }
    loop = ast.For(target=ast.Name(id="item", ctx=ast.Store()),
                   iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()),
                   body=[block, ast.parse("calls.append('claim')").body[0]], orelse=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
                 str(script), "exec"), namespace)
    assert calls == (["withheld"] if changed else ["claim"])


def test_acceptance_hook_seals_before_tests_and_does_not_self_complete(setup, monkeypatch):  # noqa:F811
    from skcapstone.fleet import production_acceptance as acceptance
    from skcapstone.fleet import production_tests

    s = setup
    s.plan_path.unlink()
    core = {"id": s.binding["source_card"], "meta": {"repository": "https://example.org/a.git"},
            "acceptance_criteria": ["Verify source regression."]}
    binding = dict(s.binding, criteria_sha256=profile.contract(core)["criteria_sha256"])
    profile.qualify_profile(s.home, core, s.policy,
        {"pytest": {"tests/test_any.py": 1}, "compile": [], "lint": [], "changelog": False},
        "operator", "b" * 64)
    review, claim = "abcd9876", "d" * 32
    exits = s.home / "evidence/production-review-exits"
    exits.mkdir(parents=True)
    (exits / f"{review}-{claim}.json").write_text("{}")
    directory = acceptance.review_directory(s.home, review, claim)
    directory.mkdir(parents=True)
    (directory / "context.json").write_text("{}")
    context = {"policy_sha256": acceptance.digest(s.policy), "test_binding": binding,
               "source_workspace": str(s.workspace),
               "source": {"card": core["id"], "revision": "source", "owner": "p",
                          "claim": "pclaim", "repository": core["meta"]["repository"]},
               "review": {"card": review, "revision": "review", "owner": "r", "claim": claim}}
    monkeypatch.setattr(acceptance, "read_json", lambda path: context)
    monkeypatch.setattr(acceptance, "terminal_guard", lambda *args: None)
    monkeypatch.setattr(acceptance, "artifacts", lambda *args: None)
    monkeypatch.setattr(acceptance, "native_state", lambda home, card: {
        "revision": "source" if card == core["id"] else "review",
        "owner": "p" if card == core["id"] else "r",
        "claim_revision": "pclaim" if card == core["id"] else claim})
    observed = []

    def tests(home, actual_binding, workspace, policy):
        value, _, _ = plan.load_plan(home, actual_binding)
        observed.append(value["checks"][0]["argv"][-1])
        return None

    monkeypatch.setattr(production_tests, "run_or_read_tests", tests)
    monkeypatch.setattr(acceptance, "finish_pair",
                        lambda *args, **kw: pytest.fail("no test proof"))
    result = acceptance.reconcile(s.home, s.policy, process_check=lambda *args: None)
    assert result == [{"card": review, "state": "awaiting-trusted-tests"}]
    assert observed == ["tests/test_any.py"]
