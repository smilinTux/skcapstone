"""tank must be rejected as SKFLEET_ONLY_SEAT at the dispatcher, not just the seat cycle.

`_ONLY_SEAT` in skfleet-rotate.py was validated only against `_SEAT_RE` (a
generic seat-name shape check), never against the lifecycle roster. `tank`
folded into `atlas` and is no longer a member of `LIFECYCLE_SEATS`, but
`_SEAT_RE` still matches the string "tank" fine, and `_load_seat_placement`
accepts a `tank` key in the manifest. So on a host still carrying an old
placement file, `SKFLEET_ONLY_SEAT=tank` would still admit `seat-tank` cards
at the dispatcher, even though `seat_cycle_entrypoint` already refuses tank
as a recurring seat. "tank cannot be dispatched" was true in one place and
false in another.

This proves `_ONLY_SEAT` is now validated against `LIFECYCLE_SEATS`
(imported from `skcapstone.lifecycle_seats`, not restated as a second
roster), lifting the exact statements out of the shipped script.
"""

from __future__ import annotations

import ast
import os
import re

from skcapstone.lifecycle_seats import LIFECYCLE_SEATS

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "fleet", "skfleet-rotate.py")


def _run_only_seat_validation(only_seat: str) -> None:
    """Exec the `_ONLY_SEAT` assignment plus its validation `if` statements.

    Raises whatever the shipped script raises (SystemExit on rejection).
    """
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    start = next(
        i
        for i, node in enumerate(tree.body)
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_ONLY_SEAT"
    )
    stmts = [tree.body[start]]
    for node in tree.body[start + 1 :]:
        if isinstance(node, ast.If):
            stmts.append(node)
        else:
            break
    assert len(stmts) >= 2, "expected the _ONLY_SEAT assignment plus at least one guard"

    seat_re_assign = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_SEAT_RE"
    )

    ns = {"re": re, "ONLY_SEAT": only_seat, "LIFECYCLE_SEATS": LIFECYCLE_SEATS}
    exec(compile(ast.Module(body=[seat_re_assign] + stmts, type_ignores=[]), SRC, "exec"), ns)


def test_tank_is_refused_as_only_seat() -> None:
    """tank matches the generic seat-name shape but is not in the roster."""
    assert "tank" not in LIFECYCLE_SEATS
    try:
        _run_only_seat_validation("tank")
    except SystemExit as exc:
        assert "SKFLEET_ONLY_SEAT" in str(exc)
    else:
        raise AssertionError("SKFLEET_ONLY_SEAT=tank must be refused")


def test_a_current_lifecycle_seat_is_accepted() -> None:
    for seat in LIFECYCLE_SEATS:
        _run_only_seat_validation(seat)  # must not raise


def test_empty_only_seat_is_accepted() -> None:
    """Empty means 'no restriction' and is unaffected by the roster check."""
    _run_only_seat_validation("")


def test_shape_invalid_seat_is_still_refused() -> None:
    """The pre-existing regex guard must still fire for a malformed name."""
    try:
        _run_only_seat_validation("Not Valid!")
    except SystemExit as exc:
        assert "SKFLEET_ONLY_SEAT" in str(exc)
    else:
        raise AssertionError("a shape-invalid seat name must still be refused")
