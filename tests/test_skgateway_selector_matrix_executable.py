"""Executable model and lane routing matrix for the fleet selector.

The launcher is deliberately loaded by AST rather than imported.  Importing it
would run a real coordination cycle, while this test must remain a pure,
deterministic contract check.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_selector_helpers() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    assignments = {
        "_LANE_ONLY_LABELS",
        "_GLM_LEVEL_DEFAULTS",
        "_GLM_LEVELS",
        "_GLM_SIZE_RE",
        "_CODEX_LEVEL_DEFAULTS",
        "_CODEX_LEVELS",
        "_QWEN_UNSUITABLE",
    }
    functions = {
        "_glm_model_for",
        "_codex_model_for",
        "lane_compatibility",
        "select_compatible_lane",
        "qwen_suitable",
        "_lane_model",
    }
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in assignments
                for target in node.targets
            )
        ) or (isinstance(node, ast.FunctionDef) and node.name in functions)
    ]
    namespace: dict[str, object] = {"os": __import__("os"), "re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert assignments | functions <= namespace.keys()
    return namespace


def _card(title: str) -> dict[str, str]:
    return {"title": title}


@pytest.mark.parametrize(
    ("size", "expected"),
    [("S", "glm-4.6"), ("M", "glm-4.6"), ("L", "glm-4.7"), ("XL", "glm-5.3")],
)
def test_glm_size_assignments_are_executable(size: str, expected: str) -> None:
    ns = _load_selector_helpers()
    assert ns["_glm_model_for"](_card(f"[SKGW-TEST][{size}] GLM work")) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("S", "sk-codex-fast"),
        ("M", "sk-codex-mid"),
        ("L", "sk-codex"),
        ("XL", "sk-codex"),
    ],
)
def test_codex_size_assignments_are_executable(size: str, expected: str) -> None:
    ns = _load_selector_helpers()
    assert ns["_codex_model_for"](_card(f"[SKGW-TEST][{size}] Codex work")) == expected


@pytest.mark.parametrize(
    ("labels", "escalation", "remaining", "expected"),
    [
        (["qwen-only"], False, {"qwen": 1, "glm": 1, "codex": 1, "escalate": 1}, "qwen"),
        (["glm-only"], False, {"qwen": 1, "glm": 1, "codex": 1, "escalate": 1}, "glm"),
        (["codex-only"], False, {"qwen": 1, "glm": 1, "codex": 1, "escalate": 1}, "codex"),
        (["escalation-only"], False, {"qwen": 1, "glm": 1, "codex": 1, "escalate": 1}, "escalate"),
        ([], True, {"qwen": 1, "glm": 1, "codex": 1, "escalate": 1}, "escalate"),
        ([], False, {"qwen": 0, "glm": 1, "codex": 1, "escalate": 1}, "glm"),
    ],
)
def test_lane_assignments_use_affinity_and_capacity(
    labels: list[str], escalation: bool, remaining: dict[str, int], expected: str
) -> None:
    ns = _load_selector_helpers()
    selected, reason = ns["select_compatible_lane"](
        labels, escalation, ["qwen", "glm", "codex", "escalate"], remaining
    )
    assert (selected, reason) == (expected, "compatible")


@pytest.mark.parametrize(
    ("labels", "remaining", "reason"),
    [
        (["codex-only"], {"qwen": 1, "glm": 1, "codex": 0, "escalate": 1}, "no-free-lane:codex"),
        (
            ["codex-only", "glm-only"],
            {"qwen": 1, "glm": 1, "codex": 1, "escalate": 1},
            "conflicting-lane-only:codex,glm",
        ),
        (
            ["escalation-only"],
            {"qwen": 1, "glm": 1, "codex": 1, "escalate": 0},
            "no-free-lane:escalate",
        ),
    ],
)
def test_capacity_and_conflict_paths_fail_closed(
    labels: list[str], remaining: dict[str, int], reason: str
) -> None:
    ns = _load_selector_helpers()
    selected, actual = ns["select_compatible_lane"](
        labels, False, ["qwen", "glm", "codex", "escalate"], remaining
    )
    assert selected is None
    assert actual == reason


def test_kimi_work_is_not_silently_routed_to_qwen() -> None:
    ns = _load_selector_helpers()
    assert ns["qwen_suitable"]({"title": "[SKGW-KIMI] validate Kimi model mapping"}) is False


def test_lane_model_preserves_size_specific_model_choice() -> None:
    ns = _load_selector_helpers()
    assert ns["_lane_model"]({"name": "glm", "model": "glm-4.6"}, _card("[L] work")) == "glm-4.7"
    assert ns["_lane_model"](
        {"name": "codex", "model": "sk-codex-mid"}, _card("[S] work")
    ) == "sk-codex-fast"
    assert ns["_lane_model"]({"name": "qwen", "model": "qwen3.8"}, _card("[XL] work")) == "qwen3.8"
