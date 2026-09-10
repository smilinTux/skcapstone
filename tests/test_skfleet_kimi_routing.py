"""Focused Kimi selector routing and fail-closed admission tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts/fleet/skfleet-rotate.py"


def _helpers() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    names = {
        "_KIMI_SIZE_RE",
        "_LANE_ONLY_LABELS",
        "_kimi_model_for",
        "lane_compatibility",
        "select_compatible_lane",
    }
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)
        )
        or (isinstance(node, ast.FunctionDef) and node.name in names)
    ]
    ns = {"re": __import__("re")}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), ns)
    return ns


@pytest.mark.parametrize("size", ["S", "M", "L"])
def test_kimi_small_through_large_use_coding_model(size: str) -> None:
    assert (
        _helpers()["_kimi_model_for"]({"title": f"[KIMI][{size}] bounded task"})
        == "kimi-for-coding"
    )


def test_kimi_xl_uses_k3() -> None:
    assert _helpers()["_kimi_model_for"]({"title": "[KIMI][XL] deep task"}) == "k3"


def test_kimi_label_is_exclusive_and_has_no_fallback() -> None:
    ns = _helpers()
    assert ns["lane_compatibility"](["kimi-suitable"], False) == (("kimi",), "required-lane:kimi")
    remaining = {"kimi": 1, "glm": 1, "codex": 1}
    selected, reason = ns["select_compatible_lane"](
        ["kimi-suitable"],
        False,
        ["kimi", "glm", "codex"],
        remaining,
        lane_health_by_name={
            "kimi": (False, "unknown"),
            "glm": (True, "healthy"),
            "codex": (True, "healthy"),
        },
    )
    assert selected is None
    assert reason == "no-compatible-healthy-lane:kimi"
    assert remaining == {"kimi": 1, "glm": 1, "codex": 1}


def test_kimi_only_conflicts_with_other_lane_requirements() -> None:
    ns = _helpers()
    assert (
        ns["lane_compatibility"](["kimi-only", "codex-only"], False)[1]
        == "conflicting-lane-only:codex,kimi"
    )
