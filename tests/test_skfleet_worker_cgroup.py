"""Deterministic tests for detached fleet worker cgroup migration."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(*names: str) -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(nodes) == set(names)
    namespace: dict[str, object] = {
        "_WORKER_UNIT_RE": re.compile(
            r"^skfleet-worker-(codex|glm|qwen|escalate)-([0-9a-f]{8})\.service$"
        ),
        "re": re,
    }
    exec(
        compile(ast.Module([nodes[name] for name in names], []), str(ROTATE), "exec"),
        namespace,
    )
    return namespace


def test_launch_command_creates_a_collected_user_service() -> None:
    functions = _load("_worker_unit_name", "_worker_launch_command")
    unit = functions["_worker_unit_name"]("codex", "3b227de2")

    assert functions["_worker_launch_command"](unit, "/workspace", "worker") == [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        "--service-type=exec",
        "--unit",
        "skfleet-worker-codex-3b227de2.service",
        "--property=KillMode=control-group",
        "--working-directory",
        "/workspace",
        "bash",
        "-lc",
        "worker",
    ]


@pytest.mark.parametrize(
    ("lane", "card"),
    [("bad", "3b227de2"), ("codex", "../../bad"), ("codex", "ABCDEF12")],
)
def test_worker_unit_identity_rejects_unbounded_values(lane: str, card: str) -> None:
    function = _load("_worker_unit_name")["_worker_unit_name"]
    with pytest.raises(ValueError, match="invalid worker unit identity"):
        function(lane, card)


def test_migration_counts_and_publishes_old_and_new_workers() -> None:
    functions = _load("_parse_worker_units", "_lane_busy", "_worker_cards")
    output = """
      skfleet-worker-codex-feedface.service loaded active running worker
      unrelated.service loaded active running other
      skfleet-worker-glm-cafebabe.service loaded active running worker
    """
    units = functions["_parse_worker_units"](output)
    lanes = [
        {"name": "codex", "prefix": "codex-auto-"},
        {"name": "glm", "prefix": "glm-auto-"},
    ]
    sessions = ["codex-auto-deadbeef", "persistent-pane"]

    assert functions["_lane_busy"](lanes[0], sessions, units) == [
        "codex-auto-deadbeef",
        "skfleet-worker-codex-feedface.service",
    ]
    assert functions["_lane_busy"](lanes[1], sessions, units) == [
        "skfleet-worker-glm-cafebabe.service"
    ]
    assert functions["_worker_cards"](sessions, units, lanes) == [
        "cafebabe",
        "deadbeef",
        "feedface",
    ]


def test_launch_keeps_exact_claim_release_traps() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert "expected-claim-revision %s --agent %s" in source
    assert 'trap "stop_beat; release_claim; idle_agent; exit 143" HUP INT TERM' in source
    assert 'trap "stop_beat; release_claim; idle_agent" EXIT' in source
    assert "subprocess.run(_worker_launch_command(unit,workspace,inner)" in source
    assert '["tmux","new-session"' not in source
