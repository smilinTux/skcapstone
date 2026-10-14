"""The dispatcher's recipe-validation accelerator is equivalent to the sealed harness.

``production_pytest_recipe.py`` is a harness module whose bytes pin every card
profile (``runtime_fingerprint``), so the speedups live in ``recipe_accel`` and are
installed at runtime. These tests hold it to the original functions.
"""

from __future__ import annotations

import hashlib
import random
import time
from pathlib import Path

import pytest

from skcapstone.fleet import production_pytest_recipe as recipe
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import recipe_accel as accel

ORIGINAL_COVERED = recipe.covered
ORIGINAL_CHECKS = recipe.recipe_checks

#: sha256 of src/skcapstone/fleet/production_pytest_recipe.py on origin/main
#: (3486f6fb). Editing a harness module stales every qualified profile on the
#: board; if you must, update this hash AND plan the requalification storm.
HARNESS_RECIPE_SHA256 = "11a6c85a7936613ab08add1f03fa49f46c418de58a5262fcbde21a7d7f444691"


@pytest.fixture(autouse=True)
def _clean():
    accel.uninstall()
    accel.cache_clear()
    yield
    accel.uninstall()


def _outcome(fn, *args):
    try:
        return ("ok", fn(*args))
    except plan.TestEvidenceError as exc:
        return ("err", str(exc))


def test_harness_module_bytes_are_untouched() -> None:
    source = Path(recipe.__file__).read_bytes()
    assert hashlib.sha256(source).hexdigest() == HARNESS_RECIPE_SHA256


def test_covered_matches_original_on_fuzzed_targets() -> None:
    rng = random.Random(1181)
    parts = ["tests", "src", "a", "b", "unit", "x.py", "y.py", "pkg"]
    for _ in range(4000):
        targets = {}
        for _ in range(rng.randint(0, 6)):
            targets["/".join(rng.choice(parts) for _ in range(rng.randint(1, 4)))] = 1
        if rng.random() < 0.1:
            targets[""] = 1
        path = "/".join(rng.choice(parts) for _ in range(rng.randint(1, 5)))
        if rng.random() < 0.05:
            path += "/"
        assert _outcome(accel.covered, path, targets) == _outcome(
            ORIGINAL_COVERED, path, targets
        ), (path, targets)


def test_covered_rejects_overlap_outside_and_py_prefix() -> None:
    assert accel.covered("tests/unit/test_b.py", {"tests/unit": 1}) == "tests/unit"
    with pytest.raises(plan.TestEvidenceError):
        accel.covered("tests/unit/test_b.py", {"tests": 1, "tests/unit": 1})
    with pytest.raises(plan.TestEvidenceError):
        accel.covered("src/other.py", {"tests": 1})
    with pytest.raises(plan.TestEvidenceError):
        accel.covered("tests/test_a.py/x", {"tests/test_a.py": 1})


def _recipe(count: int, per_dir: int = 40) -> dict:
    return {
        "pytest": {"tests/t%d/test_%d.py" % (i % per_dir, i): 1 for i in range(count)},
        "compile": [],
        "lint": [],
        "changelog": False,
        "file_cap": count,
    }


def test_install_is_idempotent_and_patches_every_binding() -> None:
    from skcapstone.fleet import production_test_profile as profile

    assert accel.install() is True
    assert accel.install() is False
    assert recipe.covered is accel.covered
    assert recipe.recipe_checks is not ORIGINAL_CHECKS
    assert profile.recipe_checks is recipe.recipe_checks
    accel.uninstall()
    assert recipe.covered is ORIGINAL_COVERED
    assert recipe.recipe_checks is ORIGINAL_CHECKS
    assert profile.recipe_checks is ORIGINAL_CHECKS


def test_cached_checks_equal_original_for_valid_and_invalid_recipes() -> None:
    accel.install()
    candidates = [
        {"pytest_all": True},
        _recipe(1),
        _recipe(37),
        dict(_recipe(5), compile=["src/a.py"], lint=["src/a.py"], changelog=True),
        dict(_recipe(5), deselect=["tests/t0/test_0.py::test_x"]),
        {"pytest": {"src/outside.py": 1}, "compile": [], "lint": [], "changelog": False},
        {
            "pytest": {"tests/a": 1, "tests/a/b.py": 1},
            "compile": [],
            "lint": [],
            "changelog": False,
        },
        {"unexpected": True},
        {"vitest": {"src/a.test.ts": 1}},
    ]
    for value in candidates:
        expected = _outcome(ORIGINAL_CHECKS, value)
        assert _outcome(recipe.recipe_checks, value) == expected
        assert _outcome(recipe.recipe_checks, value) == expected  # cached replay


def test_cache_returns_fresh_copies_and_replays_failures() -> None:
    accel.install()
    value = _recipe(8)
    first = recipe.recipe_checks(value)
    first[0]["argv"].append("--poison")
    first.append({"id": "poison"})
    second = recipe.recipe_checks(value)
    assert "--poison" not in second[0]["argv"]
    assert [check["id"] for check in second] == ["pytest"]
    assert accel.cache_stats()["hits"] >= 1

    bad = {"pytest": {"src/outside.py": 1}, "compile": [], "lint": [], "changelog": False}
    messages = set()
    for _ in range(3):
        with pytest.raises(plan.TestEvidenceError) as caught:
            recipe.recipe_checks(bad)
        messages.add(str(caught.value))
    assert len(messages) == 1
    assert accel.cache_stats()["hits"] >= 3


def test_target_order_is_part_of_the_key() -> None:
    accel.install()
    base = {"compile": [], "lint": [], "changelog": False}
    ab = dict(base, pytest={"tests/a.py": 1, "tests/b.py": 1})
    ba = dict(base, pytest={"tests/b.py": 1, "tests/a.py": 1})
    assert recipe.recipe_checks(ab)[0]["argv"][-2:] == ["tests/a.py", "tests/b.py"]
    assert recipe.recipe_checks(ba)[0]["argv"][-2:] == ["tests/b.py", "tests/a.py"]


def test_values_the_validator_distinguishes_never_share_a_cache_entry() -> None:
    accel.install()
    base = {"compile": [], "lint": [], "changelog": False}
    good = dict(base, pytest={"tests/a.py": 1})
    as_bool = dict(base, pytest={"tests/a.py": True})  # bool is not the int the validator wants
    as_tuple = dict(base, compile=("src/a.py",))  # not a list
    assert _outcome(recipe.recipe_checks, good)[0] == "ok"
    assert _outcome(recipe.recipe_checks, as_bool) == _outcome(ORIGINAL_CHECKS, as_bool)
    assert _outcome(recipe.recipe_checks, as_tuple) == _outcome(ORIGINAL_CHECKS, as_tuple)
    assert _outcome(recipe.recipe_checks, "nope") == _outcome(ORIGINAL_CHECKS, "nope")  # type: ignore[arg-type]


def test_interpreter_prefix_is_part_of_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    accel.install()
    value = _recipe(2)
    first = recipe.recipe_checks(value)[0]["argv"][0]
    monkeypatch.setattr(plan, "PREFIX", Path("/elsewhere/qualify-env"))
    second = recipe.recipe_checks(value)[0]["argv"][0]
    assert first != second and second.startswith("/elsewhere/qualify-env")


def test_large_recipe_validation_is_fast() -> None:
    """4,096 targets: the original is quadratic (several seconds); this is linear."""
    big = _recipe(4096)
    accel.install()
    started = time.perf_counter()
    checks = recipe.recipe_checks(big)
    cold = time.perf_counter() - started
    started = time.perf_counter()
    for _ in range(20):
        recipe.recipe_checks(big)
    warm = (time.perf_counter() - started) / 20
    assert len(checks[0]["argv"]) > 4096
    assert cold < 1.5, cold
    assert warm < 0.2, warm
