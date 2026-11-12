"""The one-host-per-seat invariant needs a reader-side check, not just a writer one.

`_load_seat_placement` in skfleet-rotate.py validates seat names and that
every host is a member of ROTATION_HOSTS, but never checked cardinality: a
manifest listing two distinct, valid rotation hosts for one seat was
accepted. The invariant, "every seat maps to exactly one host", was enforced
only by the producer (`generate_seat_placement_manifest`). If that invariant
is ever violated on disk (a hand edit, a bug in a future producer, a bad
merge during Syncthing conflict resolution), nothing on the reading side
catches it, and two hosts dispatch the same cards.

This proves `_load_seat_placement` now refuses a seat with more than one
host, returning the same fail-closed shape (`{}`, "manifest-hosts:<seat>")
as its other rejections, lifting the function verbatim out of the shipped
script the same way `tests/test_seat_routing.py` does.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "fleet", "skfleet-rotate.py")

HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def _load():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    names = {"_load_seat_placement"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(fns) == len(names), "expected _load_seat_placement in shipped script"
    assigns = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_SEAT_RE"
    ]
    ns = {"re": re, "json": json, "Path": Path, "ROTATION_HOSTS": HOSTS}
    exec(compile(ast.Module(body=assigns + fns, type_ignores=[]), SRC, "exec"), ns)
    return ns


def test_two_valid_hosts_for_one_seat_is_refused(tmp_path: Path) -> None:
    """Two distinct, otherwise-valid rotation hosts for one seat must fail closed."""
    ns = _load()
    manifest = tmp_path / "seat-placement.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "seats": {"link": ["chiap01", "chiap08"]}}),
        encoding="utf-8",
    )

    placement, error = ns["_load_seat_placement"](manifest)

    assert placement == {}
    assert error == "manifest-hosts:link"


def test_single_host_still_loads_normally(tmp_path: Path) -> None:
    """The cardinality check must not disturb the ordinary one-host case."""
    ns = _load()
    manifest = tmp_path / "seat-placement.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "seats": {"link": ["chiap08"]}}),
        encoding="utf-8",
    )

    placement, error = ns["_load_seat_placement"](manifest)

    assert placement == {"link": ("chiap08",)}
    assert error is None


def test_three_hosts_for_one_seat_is_also_refused(tmp_path: Path) -> None:
    ns = _load()
    manifest = tmp_path / "seat-placement.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "seats": {"mero": ["chiap01", "chiap02", "chiap03"]}}),
        encoding="utf-8",
    )

    placement, error = ns["_load_seat_placement"](manifest)

    assert placement == {}
    assert error == "manifest-hosts:mero"
