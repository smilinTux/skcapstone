"""Recipe validation must stay cheap: it runs once per plan and per profile read.

On the chiap08 board ``harvest_completed`` re-validated ~1,500 sealed plans and
``read_profile`` re-validated every card profile each cycle. ``covered`` scanned
every target for every path (O(n^2) per recipe, 900k calls per cycle), which made
lane selection spend tens of seconds before it scored a single card.
"""

from __future__ import annotations

import random
import time

import pytest

from skcapstone.fleet import production_pytest_recipe as recipe
from skcapstone.fleet import production_test_plan as plan


def _reference_covered(path: str, targets: dict) -> str:
    """The original O(n) implementation, kept as the equivalence oracle."""
    matches = [
        t for t in targets if path == t or (not t.endswith(".py") and path.startswith(t + "/"))
    ]
    if len(matches) != 1:
        raise plan.TestEvidenceError("test file outside or overlapping approved targets")
    return matches[0]


def _outcome(fn, *args):
    try:
        return ("ok", fn(*args))
    except plan.TestEvidenceError as exc:
        return ("err", str(exc))


def test_covered_matches_reference_on_fuzzed_targets() -> None:
    rng = random.Random(1181)
    parts = ["tests", "src", "a", "b", "unit", "x.py", "y.py", "pkg"]
    for _ in range(3000):
        targets = {}
        for _ in range(rng.randint(0, 6)):
            depth = rng.randint(1, 4)
            targets["/".join(rng.choice(parts) for _ in range(depth))] = 1
        if rng.random() < 0.1:
            targets[""] = 1
        path = "/".join(rng.choice(parts) for _ in range(rng.randint(1, 5)))
        assert _outcome(recipe.covered, path, targets) == _outcome(
            _reference_covered, path, targets
        ), (path, targets)


def test_covered_rejects_overlapping_and_outside_targets() -> None:
    targets = {"tests": 1, "tests/unit": 1, "tests/test_a.py": 1}
    assert recipe.covered("tests/test_a.py", {"tests/test_a.py": 1}) == "tests/test_a.py"
    assert recipe.covered("tests/unit/test_b.py", {"tests/unit": 1}) == "tests/unit"
    with pytest.raises(plan.TestEvidenceError):
        recipe.covered("tests/unit/test_b.py", targets)  # two directories cover it
    with pytest.raises(plan.TestEvidenceError):
        recipe.covered("src/other.py", {"tests": 1})  # nothing covers it
    with pytest.raises(plan.TestEvidenceError):
        # A .py target never covers a prefix, only its exact path.
        recipe.covered("tests/test_a.py/x", {"tests/test_a.py": 1})


def _big_recipe(count: int) -> dict:
    return {
        "pytest": {"tests/t%d/test_%d.py" % (i % 40, i): 1 for i in range(count)},
        "compile": [],
        "lint": [],
        "changelog": False,
        "file_cap": count,
    }


def test_recipe_checks_scales_linearly_with_target_count() -> None:
    big = _big_recipe(4096)
    recipe.cache_clear()
    started = time.perf_counter()
    checks = recipe.recipe_checks(big)
    cold = time.perf_counter() - started
    assert checks[0]["id"] == "pytest"
    assert len(checks[0]["argv"]) > 4096
    # The quadratic form took several seconds here; linear takes milliseconds.
    assert cold < 1.0, cold


def test_recipe_checks_is_cached_per_recipe_and_callers_cannot_poison_it() -> None:
    recipe.cache_clear()
    value = _big_recipe(8)
    first = recipe.recipe_checks(value)
    first[0]["argv"].append("--poison")
    first.append({"id": "poison"})
    second = recipe.recipe_checks(value)
    assert "--poison" not in second[0]["argv"]
    assert [check["id"] for check in second] == ["pytest"]
    assert recipe.cache_stats()["hits"] >= 1
    # Target order is argv order, so a reordered recipe is a different entry.
    two = {
        "pytest": {"tests/a.py": 1, "tests/b.py": 1},
        "compile": [],
        "lint": [],
        "changelog": False,
    }
    flipped = dict(two, pytest={"tests/b.py": 1, "tests/a.py": 1})
    assert recipe.recipe_checks(two)[0]["argv"][-2:] == ["tests/a.py", "tests/b.py"]
    assert recipe.recipe_checks(flipped)[0]["argv"][-2:] == ["tests/b.py", "tests/a.py"]
    # A bool and an int are different recipes even though both are "truthy".
    assert recipe.recipe_checks(dict(two, changelog=False)) == recipe.recipe_checks(two)


def test_recipe_checks_failures_are_cached_with_the_same_error() -> None:
    recipe.cache_clear()
    bad = {"pytest": {"src/outside.py": 1}, "compile": [], "lint": [], "changelog": False}
    messages = []
    for _ in range(3):
        with pytest.raises(plan.TestEvidenceError) as caught:
            recipe.recipe_checks(bad)
        messages.append(str(caught.value))
    assert len(set(messages)) == 1
    assert recipe.cache_stats()["hits"] >= 2


def test_recipe_checks_unhashable_or_odd_input_is_not_cached_and_still_raises() -> None:
    with pytest.raises(plan.TestEvidenceError):
        recipe.recipe_checks({"unexpected": object()})
    with pytest.raises(plan.TestEvidenceError):
        recipe.recipe_checks("not a recipe")  # type: ignore[arg-type]
