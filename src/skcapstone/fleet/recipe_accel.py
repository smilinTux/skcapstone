"""Dispatcher-side accelerator for test recipe validation.

Why this is not an edit to ``production_pytest_recipe.py``: that file is one of
``production_test_plan.HARNESS_MODULES``. ``runtime_fingerprint()`` hashes the bytes
of every harness module, and every qualified card profile pins that fingerprint, so
changing one byte of it stales every profile on the board and sends all of them back
through native requalification (the storm this fleet is already drowning in).
Behaviour-identical speedups therefore live here and are installed into the running
dispatcher process, leaving the sealed harness sources, and the fingerprint, as they
were.

What it fixes. Validating a recipe is a pure function of the recipe, but:

* ``covered`` scanned every target for every path, quadratic in the target count
  (up to 4,096 per recipe). One cycle called it ~900,000 times.
* ``toolchain_fingerprint`` and ``runtime_fingerprint`` re-walk the whole qualified
  site-packages tree (``rglob``, ``lstat`` and ``resolve`` per file) on every call, and
  validating one plan or profile calls them again: ~520-830 walks per cycle.
* ``recipe_checks`` re-validated the same few hundred recipes for every sealed test
  plan and every card profile on each cycle (``harvest_completed`` alone loaded about
  1,500 plans and spent 50 s here, 178-283 s under a profiler, before lane selection
  scored its first card).

Sorted JSON can also reorder a recipe's pytest target map while the sealed argv
retains its original order. The dispatcher normalizes only that target suffix
before the existing validator, and only when the full target multiset and every
command argument before it match. The immutable plan and receipt stay unchanged.

``install()`` swaps in a linear ``covered``, a per-process memo around
``recipe_checks`` and a short-lived memo around the two fingerprints (the dispatcher
process lives about one cycle, so a toolchain swapped mid-cycle is seen next cycle,
exactly as a swap one second after the walk would be). All are checked against the
originals by the equivalence tests.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from . import production_pytest_recipe as _recipe
from . import production_test_plan as _plan

_CHECKS_CACHE: dict[str, tuple[bool, Any]] = {}
_CHECKS_CACHE_MAX = 4096
_STATS = {"hits": 0, "misses": 0}
_ORIGINALS: dict[str, Callable] = {}
#: Seconds a computed fingerprint is reused. 0 disables the memo.
FINGERPRINT_TTL_SECONDS = float(os.environ.get("SKFLEET_FINGERPRINT_TTL_S", "30"))
_FINGERPRINT_NAMES = ("toolchain_fingerprint", "runtime_fingerprint")


def covered(path: str, targets: dict[str, int]) -> str:
    """Return the one explicit file/directory covering a collected source file.

    A target covers ``path`` when it is the path itself, or a directory (any target
    not ending in ``.py``) that is a proper "/"-prefix of it. Looking up the path and
    each of its prefixes is O(depth) where the original scanned every target.
    """
    matches = []
    if path in targets:
        matches.append(path)
    index = path.find("/")
    while index != -1:
        prefix = path[:index]
        if prefix in targets and not prefix.endswith(".py"):
            matches.append(prefix)
        index = path.find("/", index + 1)
    if len(matches) != 1:
        raise _plan.TestEvidenceError("test file outside or overlapping approved targets")
    return matches[0]


def _plain(value: Any) -> bool:
    """True for JSON-shaped data whose canonical text identifies it exactly.

    Exact type checks: a tuple, or a bool in place of an int, would encode like
    another value while the validator tells them apart, so those bypass the cache.
    """
    kind = type(value)
    if kind in (str, int, bool) or value is None:
        return True
    if kind is list:
        return all(_plain(item) for item in value)
    if kind is dict:
        return all(type(key) is str and _plain(item) for key, item in value.items())
    return False


def _copy(value: Any) -> Any:
    """Copy the plain list/dict/str structure checks are made of (deepcopy is 5x slower)."""
    kind = type(value)
    if kind is list:
        return [_copy(item) for item in value]
    if kind is dict:
        return {key: _copy(item) for key, item in value.items()}
    if kind in (str, int, bool) or value is None:
        return value
    return copy.deepcopy(value)


def cached(original: Callable[[dict], list[dict]]) -> Callable[[dict], list[dict]]:
    """Memoize ``recipe_checks``: a fresh copy per call, failures replayed.

    The key keeps dict order because target order is argv order, and includes the
    interpreter paths the argv embeds.
    """

    def recipe_checks(recipe: dict) -> list[dict]:
        if not _plain(recipe):
            return original(recipe)
        key = "%s\0%s\0%s" % (
            _plan.PREFIX,
            _recipe.node.NODE,
            json.dumps(recipe, separators=(",", ":")),
        )
        entry = _CHECKS_CACHE.get(key)
        if entry is None:
            _STATS["misses"] += 1
            try:
                entry = (True, original(recipe))
            except _plan.TestEvidenceError as exc:
                entry = (False, str(exc))
            if len(_CHECKS_CACHE) >= _CHECKS_CACHE_MAX:
                _CHECKS_CACHE.clear()
            _CHECKS_CACHE[key] = entry
        else:
            _STATS["hits"] += 1
        if not entry[0]:
            raise _plan.TestEvidenceError(entry[1])
        return _copy(entry[1])

    recipe_checks.__wrapped__ = original  # type: ignore[attr-defined]
    return recipe_checks


def equivalent_target_order(actual: Any, expected: Any, recipe: dict) -> bool:
    """Accept only pytest argv permutations of the exact sealed target set."""
    targets = recipe.get("pytest")
    if not isinstance(targets, dict) or not targets:
        return False
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    if len(actual) != len(expected):
        return False
    count = len(targets)
    for observed, wanted in zip(actual, expected):
        if observed == wanted:
            continue
        if (
            not isinstance(observed, dict)
            or not isinstance(wanted, dict)
            or set(observed) != {"id", "argv"}
            or set(wanted) != {"id", "argv"}
            or observed.get("id") != "pytest"
            or wanted.get("id") != "pytest"
            or not isinstance(observed.get("argv"), list)
            or not isinstance(wanted.get("argv"), list)
        ):
            return False
        left, right = observed["argv"], wanted["argv"]
        if (
            len(left) != len(right)
            or any(not isinstance(arg, str) for arg in left)
            or any(not isinstance(arg, str) for arg in right)
            or left[:-count] != right[:-count]
            or sorted(left[-count:]) != sorted(targets)
            or sorted(right[-count:]) != sorted(targets)
        ):
            return False
    return True


def target_order_validator(original: Callable) -> Callable:
    """Allow JSON key sorting to change only the order of sealed pytest targets."""

    def validate(home, binding, value, **kwargs):
        if isinstance(value, dict) and isinstance(value.get("profile"), dict):
            expected = _recipe.recipe_checks(value["profile"].get("recipe"))
            if value.get("checks") != expected and equivalent_target_order(
                value.get("checks"), expected, value["profile"].get("recipe")
            ):
                value = {**value, "checks": expected}
        return original(home, binding, value, **kwargs)

    validate.__wrapped__ = original  # type: ignore[attr-defined]
    return validate


def memoized_fingerprint(
    original: Callable[[], str],
    *,
    ttl: Callable[[], float] = lambda: FINGERPRINT_TTL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], str]:
    """Reuse a fingerprint for ``ttl`` seconds; never cache a failure.

    Keyed on the qualification prefix, which tests (and a re-pointed install) change.
    """
    memo: dict[Any, tuple[float, str]] = {}

    def fingerprint() -> str:
        window = ttl()
        key = str(_plan.PREFIX)
        now = clock()
        prior = memo.get(key)
        if window > 0 and prior is not None and 0 <= now - prior[0] < window:
            _STATS["fingerprint_hits"] = _STATS.get("fingerprint_hits", 0) + 1
            return prior[1]
        value = original()
        memo.clear()
        memo[key] = (now, value)
        _STATS["fingerprint_walks"] = _STATS.get("fingerprint_walks", 0) + 1
        return value

    fingerprint.__wrapped__ = original  # type: ignore[attr-defined]
    return fingerprint


def cache_clear() -> None:
    _CHECKS_CACHE.clear()
    _STATS.clear()
    _STATS.update(hits=0, misses=0)


def cache_stats() -> dict[str, int]:
    return dict(_STATS)


def installed() -> bool:
    return bool(_ORIGINALS)


def install() -> bool:
    """Patch the running process. Idempotent; returns True on the first install."""
    if _ORIGINALS:
        return False
    original_checks = _recipe.recipe_checks
    original_covered = _recipe.covered
    _ORIGINALS["recipe_checks"] = original_checks
    _ORIGINALS["covered"] = original_covered
    _ORIGINALS["validate_plan"] = _plan._validate_plan
    wrapper = cached(original_checks)
    # Modules bind ``recipe_checks`` by name (``from .x import recipe_checks``), so
    # every namespace holding the original must be repointed, not just the source.
    for name, module in list(sys.modules.items()):
        if not name.startswith("skcapstone.fleet.") or module is None:
            continue
        if getattr(module, "recipe_checks", None) is original_checks:
            module.recipe_checks = wrapper  # type: ignore[attr-defined]
    _recipe.covered = covered
    _plan._validate_plan = target_order_validator(_ORIGINALS["validate_plan"])
    for name in _FINGERPRINT_NAMES:
        _ORIGINALS[name] = getattr(_plan, name)
        setattr(_plan, name, memoized_fingerprint(_ORIGINALS[name]))
    return True


def uninstall() -> None:
    """Restore the originals (tests)."""
    if not _ORIGINALS:
        return
    wrapper = _recipe.recipe_checks
    for name, module in list(sys.modules.items()):
        if name.startswith("skcapstone.fleet.") and module is not None:
            if getattr(module, "recipe_checks", None) is wrapper:
                module.recipe_checks = _ORIGINALS["recipe_checks"]  # type: ignore[attr-defined]
    _recipe.covered = _ORIGINALS["covered"]
    _plan._validate_plan = _ORIGINALS["validate_plan"]
    for name in _FINGERPRINT_NAMES:
        setattr(_plan, name, _ORIGINALS[name])
    _ORIGINALS.clear()
    cache_clear()
