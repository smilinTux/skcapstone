"""Execute real dispatcher entrypoint blocks without importing its live script."""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "scripts/fleet/skfleet-rotate.py"


def entrypoint(name):
    tree = ast.parse(SOURCE.read_text())
    matches = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and ast.unparse(child.func) == name:
                matches.append(node)
                break
    assert len(matches) == 1, f"expected one top-level {name} entrypoint"
    return compile(ast.Module(body=matches, type_ignores=[]), str(SOURCE), "exec")


def check_watchdog(dry):
    calls = []
    exec(entrypoint("run_production_cycle"), {
        "DRY": dry,
        "_dispatch_writer": lambda: "niobe",
        "run_production_cycle": lambda **kwargs: calls.append(kwargs),
    })
    assert calls == ([] if dry else [{"agent": "niobe"}])


def check_builder(dry):
    calls = []
    def offer(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    exec(entrypoint("builder_dispatch.offer"), {
        "DRY": dry,
        "HOST": "chiap08",
        "_is_niobe_builder_host": lambda host: True,
        "_builder_candidates": [(0, 0, "abcd1234", {}, ["source-only"])],
        "builder_dispatch": SimpleNamespace(offer=offer, BuilderDispatchError=ValueError,
                                            decline_reason=lambda *args: "test-no-offer"),
        "default_fleet_paths": lambda: "paths",
        "fleet_store": SimpleNamespace(Writer=lambda **kwargs: kwargs),
        "log": lambda *args: None,
        "d": "unused",
    })
    assert len(calls) == (0 if dry else 1)


class RotationDrySafety(unittest.TestCase):
    def test_dry_watchdog(self):
        check_watchdog(True)

    def test_live_watchdog(self):
        check_watchdog(False)

    def test_dry_builder(self):
        check_builder(True)

    def test_live_builder(self):
        check_builder(False)


if __name__ == "__main__":
    unittest.main()
