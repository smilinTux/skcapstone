"""Operator recipe successors publish only independently verified test evidence."""

from copy import deepcopy
from pathlib import Path

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_tests as tests


def test_executed_recipe_requires_exact_passing_receipt(monkeypatch, tmp_path):
    from skcapstone.fleet.acceptance_recipe import executed_profile

    old = {
        "card": "abcd1234",
        "repository": "repo",
        "criteria_sha256": "criteria",
        "recipe": {"pytest": {"tests": 1}},
        "qualified_by": "niobe",
    }
    new = deepcopy(old)
    new.update(
        recipe={"pytest": {"tests": 1}, "deselect": ["tests/live.py::test_live"]},
        qualified_by="lumina-nor",
    )
    sealed = {
        "profile": new,
        "initial_profile_qualification": True,
        "acceptance_requalification": True,
    }
    monkeypatch.setattr(plan, "load_plan", lambda *a, **k: (sealed, Path("plan"), "sha"))
    monkeypatch.setattr(plan, "read_json", lambda p: {"workspace": str(tmp_path)})
    monkeypatch.setattr(tests, "validate_test_receipt", lambda *a: {"receipt_sha256": "passed"})
    assert executed_profile(tmp_path, {}, old, "passed") == new
    with pytest.raises(plan.TestEvidenceError, match="receipt changed"):
        executed_profile(tmp_path, {}, old, "foreign")
    for field in ("initial_profile_qualification", "acceptance_requalification"):
        sealed[field] = False
        with pytest.raises(plan.TestEvidenceError, match="recipe successor"):
            executed_profile(tmp_path, {}, old, "passed")
        sealed[field] = True
    new["repository"] = "foreign"
    with pytest.raises(plan.TestEvidenceError, match="contract"):
        executed_profile(tmp_path, {}, old, "passed")
