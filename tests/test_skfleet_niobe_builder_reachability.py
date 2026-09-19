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


def test_builder_cards_are_withheld_from_regular_lanes_while_the_builder_holds_them() -> None:
    """The withhold is keyed on an ACTIVE hold, not on eligibility.

    This assertion used to pin the PR #635 line
    ``owned = [candidate for candidate in owned if candidate[2] not in
    _builder_candidate_ids]``, which withheld every eligible card from every
    host unconditionally. Measured on chi 2026-09-19 that parked 14-16 of a
    19-20 card pool on one 4-slot node against 54 free local seats, including
    cards the builder had terminally failed and cards it could never offer.
    The offer path (Niobe's right of first refusal) is unchanged and still
    pinned below; what changed is that a candidate the builder is NOT holding
    now stays with its owning host's lanes. See
    tests/fleet/test_builder_active_hold.py for the behaviour itself.
    """
    source = ROTATE.read_text(encoding="utf-8")
    assert "_builder_candidates = [" in source
    assert "if builder_dispatch.eligible(" in source
    assert "builder_dispatch.held_card_ids(" in source
    assert "_builder_partition(" in source
    assert (
        "owned = [candidate for candidate in owned if candidate[2] not in _builder_candidate_ids]"
        not in source
    )
    assert "if _is_niobe_builder_host(HOST):" in source
    assert "for _candidate in tuple(_builder_candidates):" in source
    assert 'if _ONLY_SEAT == "niobe":' not in source
