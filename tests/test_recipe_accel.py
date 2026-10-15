"""The dispatcher's recipe-validation accelerator is equivalent to the sealed harness.

``production_pytest_recipe.py`` is a harness module whose bytes pin every card
profile (``runtime_fingerprint``) via an execution version, so the speedups live in
``recipe_accel`` and are installed at runtime. These tests hold it to the original functions.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pytest

from skcapstone.fleet import harness_attest as attest
from skcapstone.fleet import production_pytest_recipe as recipe
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import recipe_accel as accel

ORIGINAL_COVERED = recipe.covered
ORIGINAL_CHECKS = recipe.recipe_checks
ORIGINAL_PLAN_VALIDATOR = plan._validate_plan


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


def test_harness_recipe_execution_tree_matches_ledger() -> None:
    """Byte edits are free now; an execution change needs a version bump (see
    ``harness_attest``), which is what tests/fleet/test_harness_fingerprint.py guards."""
    name = "production_pytest_recipe.py"
    committed = attest.HARNESS_EXECUTION_LEDGER[plan.HARNESS_LEDGER_VERSION][name]
    assert attest.ast_digest(Path(recipe.__file__).read_bytes()) == committed


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
    assert plan._validate_plan is not ORIGINAL_PLAN_VALIDATOR
    accel.uninstall()
    assert recipe.covered is ORIGINAL_COVERED
    assert recipe.recipe_checks is ORIGINAL_CHECKS
    assert profile.recipe_checks is ORIGINAL_CHECKS
    assert plan._validate_plan is ORIGINAL_PLAN_VALIDATOR


def test_plan_validator_accepts_only_exact_pytest_target_permutations() -> None:
    recipe_value = {
        "pytest": {"tests/test_a.py": 1, "tests/test_z.py": 1},
        "compile": [],
        "lint": [],
        "changelog": False,
    }
    expected = recipe.recipe_checks(recipe_value)
    sealed = {
        "profile": {"recipe": recipe_value},
        "checks": [
            {
                **expected[0],
                "argv": [*expected[0]["argv"][:-2], "tests/test_z.py", "tests/test_a.py"],
            }
        ],
    }
    observed = []

    def original(_home, _binding, value, **_kwargs):
        observed.append(value)
        if value["checks"] != expected:
            raise plan.TestEvidenceError("checks changed")

    validate = accel.target_order_validator(original)
    validate(None, None, sealed, successor=False)
    assert observed[-1]["checks"] == expected
    assert sealed["checks"] != expected

    changed = {
        **sealed,
        "checks": [
            {**sealed["checks"][0], "argv": ["/other/python", *sealed["checks"][0]["argv"][1:]]}
        ],
    }
    with pytest.raises(plan.TestEvidenceError, match="checks changed"):
        validate(None, None, changed, successor=False)
    duplicate = {
        **sealed,
        "checks": [
            {
                **sealed["checks"][0],
                "argv": [*expected[0]["argv"][:-2], "tests/test_a.py", "tests/test_a.py"],
            }
        ],
    }
    with pytest.raises(plan.TestEvidenceError, match="checks changed"):
        validate(None, None, duplicate, successor=False)


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


# -- fingerprint memo ----------------------------------------------------------------
def _fake_qualify_env(root: Path) -> Path:
    prefix = root / "qualify-env"
    site = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = site / "site-packages"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin/ruff").write_text("ruff")
    (prefix / "bin/python").symlink_to("/usr/bin/python3")
    (prefix / "qualify-manifest.json").write_text("{}")
    for package in (
        "pytest",
        "_pytest",
        "pytest_asyncio",
        "ruff",
        "pluggy",
        "iniconfig",
        "packaging",
    ):
        (site / package).mkdir(parents=True)
        (site / package / "__init__.py").write_text("# " + package)
    return prefix


def test_fingerprint_memo_reuses_within_ttl_and_rewalks_after() -> None:
    calls = []
    now = [100.0]

    def walk() -> str:
        calls.append(now[0])
        return "fp-%d" % len(calls)

    memo = accel.memoized_fingerprint(walk, ttl=lambda: 30.0, clock=lambda: now[0])
    assert [memo(), memo(), memo()] == ["fp-1", "fp-1", "fp-1"]
    now[0] += 29
    assert memo() == "fp-1"
    now[0] += 2
    assert memo() == "fp-2"
    assert len(calls) == 2


def test_fingerprint_memo_never_caches_a_failure_and_ttl_zero_disables() -> None:
    state = {"fail": True, "calls": 0}

    def walk() -> str:
        state["calls"] += 1
        if state["fail"]:
            raise plan.TestEvidenceError("qualified runtime manifest is missing")
        return "fp"

    memo = accel.memoized_fingerprint(walk, ttl=lambda: 30.0, clock=lambda: 0.0)
    for _ in range(2):
        with pytest.raises(plan.TestEvidenceError):
            memo()
    state["fail"] = False
    assert memo() == "fp" and memo() == "fp"
    assert state["calls"] == 3

    off = accel.memoized_fingerprint(walk, ttl=lambda: 0.0, clock=lambda: 0.0)
    off()
    off()
    assert state["calls"] == 5


def test_fingerprint_memo_is_scoped_to_the_qualification_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _fake_qualify_env(tmp_path / "a")
    second = _fake_qualify_env(tmp_path / "b")
    (second / "bin/ruff").write_text("a different ruff")
    monkeypatch.setattr(plan, "PREFIX", first)
    original = plan.runtime_fingerprint
    expected_first = original()
    monkeypatch.setattr(plan, "PREFIX", second)
    expected_second = original()
    assert expected_first != expected_second

    accel.install()
    monkeypatch.setattr(plan, "PREFIX", first)
    assert plan.runtime_fingerprint() == expected_first
    assert plan.runtime_fingerprint() == expected_first
    monkeypatch.setattr(plan, "PREFIX", second)
    assert plan.runtime_fingerprint() == expected_second
    stats = accel.cache_stats()
    assert stats["fingerprint_hits"] >= 1 and stats["fingerprint_walks"] >= 2


def test_memoized_toolchain_fingerprint_equals_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = _fake_qualify_env(tmp_path)
    monkeypatch.setattr(plan, "PREFIX", prefix)

    def outcome():
        # Some runners' interpreters make the fake env "escape its base prefix";
        # the memo must then refuse identically, so compare the full outcome.
        try:
            return ("ok", plan.toolchain_fingerprint())
        except plan.TestEvidenceError as exc:
            return ("refused", str(exc))

    expected = outcome()
    accel.install()
    assert outcome() == expected
    assert outcome() == expected
    accel.uninstall()
    assert outcome() == expected


def test_uninstall_restores_the_fingerprint_functions() -> None:
    before = (plan.runtime_fingerprint, plan.toolchain_fingerprint)
    accel.install()
    assert (plan.runtime_fingerprint, plan.toolchain_fingerprint) != before
    accel.uninstall()
    assert (plan.runtime_fingerprint, plan.toolchain_fingerprint) == before
