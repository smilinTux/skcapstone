"""The skip ledger must refer to tests that exist.

A ledger entry that matches nothing is the very defect the ledger exists to
prevent, one level up: it LOOKS like a declaration, so a reader believes that
skip is accounted for, while it silently covers no test at all. Four of the
seventeen entries were mis-keyed when first written (a test that lives in a
class, two parametrized ids, one renamed test), and every one of them would
have read as "declared".

These checks are static -- they do not collect the suite -- so they stay fast
and work even when the gated dependency is absent, which is precisely the
environment where the ledger matters most.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEDGER = PROJECT_ROOT / "tests" / "skip_ledger.txt"


def _entries() -> list[str]:
    return [
        line.strip()
        for line in LEDGER.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_ledger_exists_and_is_not_empty():
    """An empty ledger would make every other check here vacuously true."""
    assert LEDGER.is_file(), f"{LEDGER} is missing"
    assert _entries(), "the skip ledger declares nothing; it cannot be certifying anything"


def _valid_node_paths(path: Path) -> set[str]:
    """Every nodeid path the file can actually produce, via AST.

    Grepping for the NAME is not enough, and that is not a hypothetical: the
    first version of this check grepped, and it PASSED the real typo it was
    written to catch. `test_missing_registry_returns_none_never_crashes` is a
    METHOD of TestRegistryRoleFold, so its nodeid needs the class segment --
    but the bare name does appear in the file, so a grep says yes. A check
    that passes the exact bug it was built for is worth nothing; it has to
    model the nodeid, not the text.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    paths: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            paths.add(node.name)
        elif isinstance(node, ast.ClassDef):
            paths.add(node.name)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    paths.add(f"{node.name}::{sub.name}")
    return paths


@pytest.mark.parametrize("entry", _entries())
def test_every_ledger_entry_names_a_real_test(entry: str):
    """The file exists, and the full nodeid path resolves inside it."""
    path_part, _, node = entry.partition("::")
    path = PROJECT_ROOT / path_part
    assert path.is_file(), f"ledger entry points at a file that does not exist: {path_part}"
    if not node:
        return  # whole-module entry (a module-level skip)

    # Drop a trailing parametrize id / wildcard segment: that is nodeid syntax,
    # not a python name. `Class::*[skvoice]` validates as `Class`.
    segments = [re.sub(r"\[.*?\]$", "", seg).replace("*", "").strip() for seg in node.split("::")]
    segments = [seg for seg in segments if seg]
    if not segments:
        return
    wanted = "::".join(segments)

    valid = _valid_node_paths(path)
    near = sorted(v for v in valid if segments[-1] in v)[:3] or sorted(valid)[:3]
    assert wanted in valid, (
        f"ledger entry `{entry}` resolves to `{wanted}`, which {path_part} does not "
        f"define. Did you mean one of: {near}? A ledger entry that matches nothing "
        "reads as a declaration while covering no test."
    )


def test_wildcard_matcher_handles_a_parametrize_id():
    """Regression: fnmatch reads `[skvoice]` as a character class.

    Using fnmatch here matched ZERO of the 16 skvoice nodeids while looking
    like it worked, which would have let them through as declared.
    """
    from tests.conftest import _matches

    nodeid = "tests/test_integration_backbone.py::TestStandaloneMode::test_x[skvoice]"
    assert _matches(nodeid, "tests/test_integration_backbone.py::TestStandaloneMode::*[skvoice]")
    assert not _matches(nodeid, "tests/test_integration_backbone.py::TestStandaloneMode::*[skmem]")
    assert _matches(nodeid, "tests/test_integration_backbone.py")
    assert not _matches(nodeid, "tests/test_other.py")
