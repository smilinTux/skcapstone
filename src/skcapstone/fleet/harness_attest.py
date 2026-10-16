"""Execution-semantics attestation for the sealed test harness.

``production_test_plan.runtime_fingerprint`` pins every qualified test profile. It
used to hash the raw bytes of each ``HARNESS_MODULES`` file, so a comment, a rename
of plan naming, or any unrelated fix restaled the whole board and forced native
requalification. It now pins ``HARNESS_EXECUTION_VERSION`` instead, and this module
is the guard that keeps that integer honest.

``ast_digest`` hashes a module's parse tree with comments, docstrings, formatting
and the two bookkeeping constants removed. ``HARNESS_EXECUTION_LEDGER`` records the
digest of every module for each execution version. The guard test fails when the
tree on disk no longer matches the ledger row of the current version, so:

* execution change (what runs, how, interpreter, selection, sandbox, evidence
  format): bump ``HARNESS_EXECUTION_VERSION``, add a ledger row. Profiles restale,
  on purpose.
* behaviour-neutral change that still alters the parse tree (renamed helper, moved
  plan naming, acceptance plumbing): refresh the current version's row in the same
  PR. The reviewer attests the change does not alter test execution. No restale.
* comment, docstring or formatting change: nothing to do.

This module is intentionally NOT a harness module and is not fingerprinted.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

#: Top-level names that are bookkeeping about the fingerprint itself. Excluded so a
#: version bump or baseline edit does not need a self-referential digest.
BOOKKEEPING_NAMES = frozenset({"HARNESS_EXECUTION_VERSION", "_LEGACY_HARNESS_SHA256"})


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]


def _is_bookkeeping(node: ast.stmt) -> bool:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return False
    return any(isinstance(t, ast.Name) and t.id in BOOKKEEPING_NAMES for t in targets)


def _canon(node: object) -> object:
    if isinstance(node, ast.AST):
        fields = {}
        for name in node._fields:
            value = getattr(node, name, None)
            if value is None or value == []:
                continue  # fields added by newer Pythons stay invisible when empty
            fields[name] = _canon(value)
        return [type(node).__name__, fields]
    if isinstance(node, list):
        return [_canon(item) for item in node]
    if isinstance(node, bytes):
        return {"bytes": node.hex()}
    if isinstance(node, (complex, float)) or node is Ellipsis:
        return {"repr": repr(node)}
    return node


def ast_digest(source: str | bytes) -> str:
    """Digest of the execution-relevant parse tree of one module."""
    tree = ast.parse(source)
    tree.body = [stmt for stmt in tree.body if not _is_bookkeeping(stmt)]
    _strip_docstrings(tree)
    blob = json.dumps(_canon(tree), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def module_digests(root: Path, names: tuple[str, ...]) -> dict[str, str]:
    return {name: ast_digest((root / name).read_bytes()) for name in names}


#: execution version -> {harness module: ast_digest}. Append a row on every bump.
HARNESS_EXECUTION_LEDGER: dict[int, dict[str, str]] = {
    1: {
        "qualified_runtime.py": (
            "cd3954fc8ece6de4b081df8f3f29131ba48471c6d4a197eeb5b1be3098d364a3"
        ),
        "production_pytest_recipe.py": (
            "923c9560e5c4d6d99c32415ee505c784694f40c9dd987d34b2044808a0432f1e"
        ),
        "production_pytest_selection.py": (
            "5031458732eab907885333d1729e76eeb47e29c5f41b96e057097f89574fbec2"
        ),
        "production_test_plan.py": (
            "5b8d84bfb84b6209bcce7a74b9fcfa6c69b16bc0db6540bc49d9be4e44555186"
        ),
        "production_test_profile.py": (
            "72c49974efcd6cb7d80657f7ea3cf661393d1756d06a92e11c123ea1abc8c800"
        ),
        "production_test_worker.py": (
            "67e7eb09c8fde500ef32eba43a8828b46b87881be056daa9804a79138a3eb57b"
        ),
        "production_test_node.py": (
            "8956d5de6eb6c5af5356a710035d87e6c83cc14d69747cfefa7a3c094641b5e0"
        ),
        "production_node_test_recipe.py": (
            "85ea669b2f78366814f306f33e81b6abb0239ee2544930d673e178fa04c2892b"
        ),
        "production_test_composite.py": (
            "630d908322d27ab21dc766b5f570ac9ab86fcc50adbe69d5eb265a375ba4edd3"
        ),
        "production_tests.py": (
            "16ee718b5440589f4984eba8b1695a85903864c5691a446d322c3df8fa56fc63"
        ),
        "production_admission.py": (
            "90b7fafcdd58a098389d10eee4e9f9eab3a9a72782b527dbe3651737a186e9d2"
        ),
        "production_builder.py": (
            "a7ab24eeeebe81710bdc0358c946cfd49ea7d101c6f4ea805f59012bb3b460af"
        ),
        "production_policy.py": (
            "e1fc577cd7f961d9f6280e685f097f9b13b6335ddf3ae3aa0adc1aaf1062c8a4"
        ),
    },
}
