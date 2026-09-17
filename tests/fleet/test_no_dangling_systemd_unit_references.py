"""Every skfleet-*.service / skfleet-*.timer literal must name a shipped unit.

Two rounds of manual hunting for tank's dangling systemd references (spec
3.6 folded the tank seat into atlas and deleted skfleet-tank.service /
skfleet-tank.timer from src/skcapstone/data/systemd/) both came up short:
GOVERNED_SEAT_SERVICES and approved_legacy_timers in fleet/timer_enablement.py
and _TANK in fleet/seat_cycle_orchestrator.py all kept naming units that no
longer ship. Three rounds of that is a signal that the missing piece is a
check, not more care, so this test discovers every skfleet unit literal in
src/ and scripts/ instead of listing the ones already found by hand: a
future dangling reference (to a retired seat or a renamed unit) fails here
before it ships.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_DATA_DIR = REPO_ROOT / "src" / "skcapstone" / "data" / "systemd"
SCAN_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "scripts")

_UNIT_PATTERN = re.compile(r"skfleet-[A-Za-z0-9_-]*\.(?:service|timer)")

#: Units that are real, live, and referenced by name, but are deliberately
#: never templated under src/skcapstone/data/systemd/: skfleet-rotate.service
#: (and its timer) are hand-installed per host at
#: ~/.config/systemd/user/skfleet-rotate.service, documented in
#: docs/fleet/model-lane-routing.md. That is a different install path from
#: the packaged lifecycle-seat units this guard exists to protect, so it is
#: named here explicitly rather than silently widening the shipped-unit
#: check to satisfy it.
ALLOWED_UNSHIPPED_UNITS = frozenset({"skfleet-rotate.service", "skfleet-rotate.timer"})


def _shipped_units() -> frozenset[str]:
    return frozenset(
        path.name for path in SYSTEMD_DATA_DIR.iterdir() if path.suffix in {".service", ".timer"}
    )


_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """id()s of the Constant nodes that are module/class/function docstrings.

    Prose describing a *past* incident ("X shipped in exactly that state and
    nobody noticed") reads as an ordinary string literal to the AST, but it
    is documentation, not a live reference the code depends on, so it must
    not be asserted against the shipped-unit inventory.
    """

    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_OWNERS):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                ids.add(id(node.body[0].value))
    return ids


def _referenced_units() -> list[tuple[str, int, str]]:
    """Every skfleet unit literal in a real string constant under src/ and scripts/.

    Walking the AST for ``ast.Constant`` string nodes (rather than
    regex-scanning raw file text) means a comment merely *talking about* a
    unit name, and an f-string fragment that only becomes a unit name after
    interpolation, cannot trip this check either falsely or silently: only a
    literal string a human actually wrote as data is asserted against
    reality. Docstrings are prose and are excluded the same way.
    """

    found: list[tuple[str, int, str]] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            docstring_ids = _docstring_constant_ids(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if id(node) in docstring_ids:
                    continue
                for match in _UNIT_PATTERN.finditer(node.value):
                    found.append((str(path.relative_to(REPO_ROOT)), node.lineno, match.group()))
    return found


def test_every_referenced_skfleet_unit_is_shipped() -> None:
    shipped = _shipped_units()
    dangling = [
        (source, lineno, unit)
        for source, lineno, unit in _referenced_units()
        if unit not in shipped and unit not in ALLOWED_UNSHIPPED_UNITS
    ]
    assert (
        not dangling
    ), "referenced but not shipped in src/skcapstone/data/systemd/:\n" + "\n".join(
        f"  {source}:{lineno}: {unit}" for source, lineno, unit in sorted(dangling)
    )


@pytest.mark.parametrize("unit", sorted(ALLOWED_UNSHIPPED_UNITS))
def test_allowlisted_units_are_still_actually_referenced(unit: str) -> None:
    """An allowlist entry nobody references anymore is dead weight, not a guard."""

    assert any(found_unit == unit for _source, _lineno, found_unit in _referenced_units())
