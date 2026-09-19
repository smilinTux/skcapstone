"""A codex-pinned card that escalates must still have a lane."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {"lane_compatibility"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert fns, "lane_compatibility not found at module level"
    assigns = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "_LANE_ONLY_LABELS" for t in n.targets)
    ]
    ns: dict[str, object] = {}
    mod = ast.Module(body=assigns + fns, type_ignores=[])
    exec(compile(mod, str(ROTATE), "exec"), ns)
    return ns["lane_compatibility"]


def test_codex_pinned_escalation_routes_to_the_escalate_lane():
    lane_compatibility = _load()
    lanes, reason = lane_compatibility({"codex-only"}, escalation_required=True)
    assert lanes == ("escalate",), (lanes, reason)
    assert reason == "required-lane:escalate", reason


def test_a_non_codex_pin_still_conflicts_with_escalation():
    lane_compatibility = _load()
    for pin in ("qwen-only", "glm-only", "kimi-only"):
        lanes, reason = lane_compatibility({pin}, escalation_required=True)
        assert lanes == (), (pin, lanes, reason)
        assert reason.startswith("conflicting-lane-only:"), (pin, reason)


def test_plain_codex_pin_without_escalation_is_unchanged():
    lane_compatibility = _load()
    lanes, reason = lane_compatibility({"codex-only"})
    assert lanes == ("codex",), (lanes, reason)
    assert reason == "required-lane:codex", reason
