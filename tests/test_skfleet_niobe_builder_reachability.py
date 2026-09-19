"""Niobe placement is the single reachable builder dispatch authority."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _helper():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_is_niobe_builder_host"
    )
    namespace = {
        "_SEAT_PLACEMENT": {},
        "_SEAT_PLACEMENT_ERROR": None,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_is_niobe_builder_host"]


def test_only_public_niobe_placement_is_builder_authority() -> None:
    helper = _helper()
    placement = {"niobe": ("chiap08",)}
    assert helper("chiap08", placement, None)
    assert not helper("chiap04", placement, None)
    assert not helper("chiap08", placement, "manifest-unavailable")
    assert not helper("chiap08", {}, None)


def test_builder_cards_are_withheld_from_regular_lanes() -> None:
    """Builder candidates leave the local lanes unless the builder refuses them.

    This assertion used to pin the withholding as UNCONDITIONAL:

        owned = [candidate for candidate in owned
                 if candidate[2] not in _builder_candidate_ids]

    That is what idled the fleet. Measured on chi 2026-09-19 across all five
    hosts, every card in the 21-card pool was a builder candidate, so every
    host logged `owned=0 reason=builder-path-withheld` with 65 free seats
    between them while the single builder node worked 4 at a time.

    The withholding itself is still the safety mechanism and is still pinned
    here: a candidate the builder might take must not reach a lane. What moved
    is that a candidate the builder will NEVER take is no longer withheld from
    everybody. The split lives in builder_dispatch.durable_decline(), and
    tests/fleet/test_builder_withholding.py proves a released card cannot also
    be offered.
    """
    source = ROTATE.read_text(encoding="utf-8")
    assert "_builder_candidates = [" in source
    assert "if builder_dispatch.eligible(" in source
    assert "builder_dispatch.partition_withheld(" in source
    assert (
        "if candidate[2] not in _builder_candidate_ids\n"
        "    or candidate[2] in _builder_withheld_set" in source
    )
    assert "if _is_niobe_builder_host(HOST):" in source
    assert "for _candidate in tuple(_builder_candidates):" in source
    assert 'if _ONLY_SEAT == "niobe":' not in source
