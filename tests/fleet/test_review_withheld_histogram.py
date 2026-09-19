"""The withheld-review log must show the shape of the withholding, not just a count."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(name: str):
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert functions, f"{name} is not defined at module level in {ROTATE.name}"
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace[name]


def test_counts_every_reason_not_just_the_first_twelve_cards():
    histogram = _load("_review_withheld_reason_histogram")
    withheld = [("%08x" % i, "dependency-blocker") for i in range(20)]
    withheld += [("aaaa0001", "wrong-seat,absent-typed-metadata")]
    assert histogram(withheld) == "dependency-blocker=20,absent-typed-metadata=1,wrong-seat=1"


def test_orders_by_weight_then_name_so_the_line_is_stable():
    histogram = _load("_review_withheld_reason_histogram")
    withheld = [
        ("00000001", "void"),
        ("00000002", "done"),
        ("00000003", "done"),
        ("00000004", "absent-source-binding"),
        ("00000005", "absent-source-binding"),
    ]
    assert histogram(withheld) == "absent-source-binding=2,done=2,void=1"


def test_empty_and_blank_reasons_do_not_invent_entries():
    histogram = _load("_review_withheld_reason_histogram")
    assert histogram([]) == ""
    assert histogram([("00000001", " , ,")]) == ""
