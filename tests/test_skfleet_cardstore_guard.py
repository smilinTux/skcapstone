"""The fleet Pi boundary keeps structural CardStore files read-only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "fleet" / "pi-cardstore-guard.mjs"
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _node(expression: str) -> object:
    script = f"""
import {{ {expression.split('(')[0]} }} from {json.dumps(GUARD.as_uri())};
console.log(JSON.stringify({expression}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _hook(event: dict[str, object]) -> object:
    script = f"""
import guard from {json.dumps(GUARD.as_uri())};
let hook;
guard({{ on(name, callback) {{ if (name === "tool_call") hook = callback; }} }});
const result = await hook({json.dumps(event)});
console.log(JSON.stringify(result));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_exact_incident_bash_append_is_blocked_before_execution() -> None:
    command = (
        "cat /home/skuser01/.skcapstone/fleet/workspaces/worker/verdict_event.json >> "
        "/home/skuser01/.skcapstone/cards/7b7c990f/events/"
        "pi-glm-chiap04-7b7c990f@chiap04.jsonl"
    )
    verdict = _hook({"toolName": "bash", "input": {"command": command}})
    assert verdict == {
        "block": True,
        "terminate": True,
        "reason": "Direct CardStore event-file mutation is forbidden. Use skcapstone coord.",
    }


def test_builtin_write_and_edit_cannot_mutate_structural_events() -> None:
    event = "/home/skuser01/.skcapstone/cards/7b7c990f/events/worker@host.jsonl"
    for tool_name in ("write", "edit"):
        verdict = _hook({"toolName": tool_name, "input": {"path": event}})
        assert isinstance(verdict, dict)
        assert verdict["block"] is True
        assert verdict["terminate"] is True


def test_read_only_event_inspection_and_evidence_writes_remain_available() -> None:
    event = "/home/skuser01/.skcapstone/cards/7b7c990f/events/worker@host.jsonl"
    evidence = "/home/skuser01/.skcapstone/evidence/work/7b7c990f/report.json"
    assert _node(f"isStructuralEventMutation({json.dumps(f'cat {event}')})") is False
    assert _node(f"isStructuralEventPath({json.dumps(event)}, '/home/skuser01')") is True
    assert _node(f"isStructuralEventPath({json.dumps(evidence)}, '/home/skuser01')") is False


def test_launcher_loads_guard_for_every_pi_worker() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert '"pi-cardstore-guard.mjs"' in source
    assert "--approve --extension %s --name %s" in source
    assert "missing Pi CardStore write guard" in source
