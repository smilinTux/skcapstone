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
* ``recipe_checks`` re-validated the same few hundred recipes for every sealed test
  plan and every card profile on each cycle (``harvest_completed`` alone loaded about
  1,500 plans and spent 50 s here, 178-283 s under a profiler, before lane selection
  scored its first card).

``install()`` swaps in a linear ``covered`` and a per-process memo around
``recipe_checks``. Both are checked against the originals by the equivalence tests.
"""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable
from typing import Any

from . import production_pytest_recipe as _recipe
from . import production_test_plan as _plan

_CHECKS_CACHE: dict[str, tuple[bool, Any]] = {}
_CHECKS_CACHE_MAX = 4096
_STATS = {"hits": 0, "misses": 0}
_ORIGINALS: dict[str, Callable] = {}


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


def cache_clear() -> None:
    _CHECKS_CACHE.clear()
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
    wrapper = cached(original_checks)
    # Modules bind ``recipe_checks`` by name (``from .x import recipe_checks``), so
    # every namespace holding the original must be repointed, not just the source.
    for name, module in list(sys.modules.items()):
        if not name.startswith("skcapstone.fleet.") or module is None:
            continue
        if getattr(module, "recipe_checks", None) is original_checks:
            module.recipe_checks = wrapper  # type: ignore[attr-defined]
    _recipe.covered = covered
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
    _ORIGINALS.clear()
    cache_clear()
