"""Every seat in LIFECYCLE_SEATS is provisioned once the manifest is generated.

`atlas` was absent from the hand-maintained `seat-placement.json`, so
`_seat_is_provisioned` reported it unprovisioned and it ran as a timer. The
fleet path itself does not hardcode a seat roster or seat count: it is driven
entirely by what the manifest declares. This proves that once the manifest is
built from `LIFECYCLE_SEATS` (via `generate_seat_placement_manifest`), every
roster seat is provisioned and an unknown seat name still is not.

The roster is never spelled out here. `LIFECYCLE_SEATS` is imported and
iterated so a future fold (as happened when tank folded into atlas) flows
through without touching this test.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path

from skcapstone.lifecycle_seats import LIFECYCLE_SEATS, write_seat_placement_manifest

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "fleet", "skfleet-rotate.py")

HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def _load_seat_functions(seat_placement_path: str):
    """Lift `_load_seat_placement` and `_seat_is_provisioned` out of the
    shipped script, the same way `tests/test_seat_routing.py` does, so this
    tests the source that runs rather than a paraphrase of it.
    """
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    names = {"_load_seat_placement", "_seat_is_provisioned"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(fns) == len(names), "expected seat placement functions in shipped script"
    assigns = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_SEAT_RE"
    ]
    ns = {
        "re": re,
        "json": json,
        "hashlib": hashlib,
        "os": os,
        "Path": Path,
        "ROTATION_HOSTS": HOSTS,
    }
    exec(compile(ast.Module(body=assigns + fns, type_ignores=[]), SRC, "exec"), ns)
    placement, error = ns["_load_seat_placement"](seat_placement_path)
    ns["_SEAT_PLACEMENT"] = placement
    ns["_SEAT_PLACEMENT_ERROR"] = error
    return ns


def test_every_lifecycle_seat_is_provisioned(tmp_path: Path) -> None:
    manifest_path = tmp_path / "seat-placement.json"
    write_seat_placement_manifest(manifest_path, active_host="chiap08", home=tmp_path)

    ns = _load_seat_functions(str(manifest_path))
    assert ns["_SEAT_PLACEMENT_ERROR"] is None

    for seat in LIFECYCLE_SEATS:
        assert ns["_seat_is_provisioned"](seat), f"seat {seat} must be provisioned"


def test_unknown_seat_name_is_still_rejected(tmp_path: Path) -> None:
    manifest_path = tmp_path / "seat-placement.json"
    write_seat_placement_manifest(manifest_path, active_host="chiap08", home=tmp_path)

    ns = _load_seat_functions(str(manifest_path))
    assert not ns["_seat_is_provisioned"]("ghost")
    assert "ghost" not in LIFECYCLE_SEATS


def test_generated_manifest_names_only_roster_seats(tmp_path: Path) -> None:
    """The manifest never grows a seat that is not in the roster, either."""
    manifest_path = tmp_path / "seat-placement.json"
    write_seat_placement_manifest(manifest_path, active_host="chiap08", home=tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(payload["seats"]) == LIFECYCLE_SEATS
