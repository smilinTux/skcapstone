"""Executable 12-case model and lane routing matrix.

The selector is a script with runtime side effects, so these tests extract only
its pure routing helpers. No board, worker, network, or runtime state is used.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
LANES = ("qwen", "glm", "codex", "escalate")


def _namespace() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    functions = {
        "_dependency_value",
        "_fold_claimability",
        "semantic_stage_completed",
        "qwen_first_exclusive",
        "qwen_suitable",
        "lane_compatibility",
        "select_compatible_lane",
    }
    assignments = {"_LANE_ONLY_LABELS", "_SEMANTIC_COMPLETE_ACTION", "_QWEN_UNSUITABLE"}
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in assignments
                for target in node.targets
            )
        )
        or (isinstance(node, ast.FunctionDef) and node.name in functions)
    ]
    namespace: dict[str, object] = {
        "re": re,
        "os": os,
        "CARDS": "/missing",
        "event_rows": lambda _card_id: [],
        "_load_evidence_events": lambda: {},
        "_fold_key": lambda value: str(value),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


def _health() -> dict[str, tuple[bool, str]]:
    return {lane: (True, "healthy") for lane in LANES}


def _capacity(**overrides: int) -> dict[str, int]:
    values = {lane: 1 for lane in LANES}
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    (
        "name",
        "labels",
        "escalation",
        "order",
        "capacity",
        "qwen_allowed",
        "exclusive",
        "health",
        "expected",
    ),
    [
        (
            "ordinary qwen",
            [],
            False,
            LANES,
            _capacity(),
            True,
            False,
            _health(),
            ("qwen", "compatible"),
        ),
        (
            "Kimi title excludes qwen",
            [],
            False,
            LANES,
            _capacity(),
            False,
            False,
            _health(),
            ("glm", "compatible"),
        ),
        (
            "Kimi explicit qwen opt-in",
            ["qwen-suitable"],
            False,
            LANES,
            _capacity(),
            True,
            False,
            _health(),
            ("qwen", "compatible"),
        ),
        (
            "qwen-first exclusive free",
            ["qwen-first"],
            False,
            ("glm", "qwen", "codex", "escalate"),
            _capacity(),
            True,
            True,
            _health(),
            ("qwen", "compatible"),
        ),
        (
            "qwen-first exclusive full",
            ["qwen-first"],
            False,
            LANES,
            _capacity(qwen=0),
            True,
            True,
            _health(),
            (None, "no-free-lane:qwen"),
        ),
        (
            "qwen-first after semantic completion",
            ["qwen-first"],
            False,
            LANES,
            _capacity(),
            True,
            False,
            _health(),
            ("qwen", "compatible"),
        ),
        (
            "codex only",
            ["codex-only"],
            False,
            LANES,
            _capacity(),
            True,
            False,
            _health(),
            ("codex", "compatible"),
        ),
        (
            "glm only",
            ["glm-only"],
            False,
            LANES,
            _capacity(),
            True,
            False,
            _health(),
            ("glm", "compatible"),
        ),
        (
            "explicit escalation",
            ["escalation-only"],
            False,
            LANES,
            _capacity(),
            True,
            False,
            _health(),
            ("escalate", "compatible"),
        ),
        (
            "boolean escalation",
            [],
            True,
            LANES,
            _capacity(),
            True,
            False,
            _health(),
            ("escalate", "compatible"),
        ),
        (
            "conflicting requirements",
            ["codex-only", "glm-only"],
            False,
            LANES,
            _capacity(),
            True,
            False,
            _health(),
            (None, "conflicting-lane-only:codex,glm"),
        ),
        (
            "required glm unhealthy",
            ["glm-only"],
            False,
            LANES,
            _capacity(),
            True,
            False,
            {**_health(), "glm": (False, "backend-down")},
            (None, "no-compatible-healthy-lane:glm"),
        ),
    ],
)
def test_routing_matrix(
    name, labels, escalation, order, capacity, qwen_allowed, exclusive, health, expected
):
    namespace = _namespace()
    if name == "Kimi title excludes qwen":
        qwen_allowed = namespace["qwen_suitable"]({"title": "[SKGW-KIMI-QUEUE] expose"}, [])
    if name == "qwen-first after semantic completion":
        namespace["event_rows"] = lambda _card_id: [
            {"action": "semantic_stage_complete", "artifact_sha256": "a" * 64}
        ]
        exclusive = namespace["qwen_first_exclusive"]("matrix", labels)
    selected = namespace["select_compatible_lane"](
        labels, escalation, list(order), dict(capacity), qwen_allowed, exclusive, health
    )
    assert selected == expected, name


def test_ordinary_no_capacity_fails_closed_without_lane_consumption():
    namespace = _namespace()
    capacity = _capacity(qwen=0, glm=0, codex=0, escalate=0)
    selected = namespace["select_compatible_lane"](
        [], False, list(LANES), capacity, True, False, _health()
    )
    assert selected == (None, "no-free-lane:qwen,glm,codex")
    assert capacity == _capacity(qwen=0, glm=0, codex=0, escalate=0)
