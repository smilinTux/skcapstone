"""Fleet workers use the mediated coordination write surface."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands
from skcapstone.codex_setup import ensure_pi_setup, render_agents_block, render_loader_script
from skcapstone.crush_integration import generate_soul_instructions
from skcapstone.lightweight import mandate_template

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "fleet" / "pi-cardstore-guard.mjs"
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
STATIC_CHECK = ROOT / "scripts" / "check-coord-write-guidance.py"


def _hook(event: dict[str, object], *, env: dict[str, str] | None = None) -> object:
    script = f"""
import guard from {json.dumps(GUARD.as_uri())};
let hook;
guard({{ on(name, callback) {{ if (name === "tool_call") hook = callback; }} }});
console.log(JSON.stringify((await hook({json.dumps(event)})) ?? null));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return json.loads(result.stdout)


def test_current_fleet_card_cannot_be_reclaimed_but_worker_continues() -> None:
    verdict = _hook(
        {
            "toolName": "bash",
            "input": {"command": "skcapstone coord claim deadbeef --agent worker"},
        },
        env={"SKFLEET_CARD_ID": "deadbeef"},
    )
    assert verdict == {
        "block": True,
        "terminate": False,
        "reason": (
            "This fleet card is already claimed at the dispatched revision. "
            "Continue without claiming it again."
        ),
    }


def test_other_card_claim_and_nonfleet_claim_remain_available() -> None:
    event = {
        "toolName": "bash",
        "input": {"command": "skcapstone coord claim feedbeef --agent worker"},
    }
    assert _hook(event, env={"SKFLEET_CARD_ID": "deadbeef"}) is None
    assert _hook(event, env={"SKFLEET_CARD_ID": ""}) is None


def _main() -> click.Group:
    @click.group()
    def main() -> None:
        pass

    register_coord_commands(main)
    return main


def test_native_and_shell_mutations_fail_closed_for_both_event_stores() -> None:
    home = Path.home()
    paths = (
        str(home / ".skcapstone/cards/deadbeef/events/worker@host.jsonl"),
        str(home / ".skcapstone/coordination/card_events/host.jsonl"),
    )
    for path in paths:
        for event in (
            {"toolName": "write", "input": {"path": path}},
            {"toolName": "edit", "input": {"path": path}},
            {"toolName": "bash", "input": {"command": f"printf x >> {path}"}},
            {
                "toolName": "bash",
                "input": {
                    "command": (
                        f'python -c "from pathlib import Path; ' f"Path('{path}').unlink()\""
                    )
                },
            },
        ):
            verdict = _hook(event)
            assert verdict == {
                "block": True,
                "terminate": True,
                "reason": "Direct CardStore JSONL mutation is forbidden. Use skcapstone coord.",
            }


def test_read_only_cli_and_non_card_evidence_writes_remain_available() -> None:
    event = str(Path.home() / ".skcapstone/cards/deadbeef/events/worker@host.jsonl")
    assert (
        _hook(
            {
                "toolName": "bash",
                "input": {"command": "skcapstone coord kanban --json"},
            }
        )
        is None
    )
    assert _hook({"toolName": "bash", "input": {"command": f"cat {event}"}}) is None
    assert (
        _hook(
            {"toolName": "write", "input": {"path": "/home/test/.skcapstone/evidence/report.json"}}
        )
        is None
    )


def test_launcher_loads_guard_and_brief_states_complete_boundary() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert '"pi-cardstore-guard.mjs"' in source
    assert '"%s --approve --extension %s --name %s "' in source
    assert "Use skcapstone coord for every verdict" in source
    assert "Never create, " in source
    assert "append, rewrite, rename, or delete CardStore JSONL" in source


def test_coord_briefing_places_boundary_before_protocol(tmp_path: Path) -> None:
    result = CliRunner().invoke(_main(), ["coord", "briefing", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Coordination Write Boundary")
    assert "All verdict, evidence, status, claim, label, dependency" in result.output
    assert "Raw file inspection is emergency operator diagnostics only" in result.output


def test_static_check_rejects_positive_raw_write_instruction(tmp_path: Path) -> None:
    launcher = tmp_path / "launcher.sh"
    launcher.write_text(
        "append the verdict to cards/deadbeef/events/worker.jsonl\n", encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location("coord_write_check", STATIC_CHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TARGETS = (tmp_path,)
    assert module.main() == 1


def test_static_check_accepts_coord_instruction(tmp_path: Path) -> None:
    launcher = tmp_path / "launcher.sh"
    launcher.write_text(
        "skcapstone coord link deadbeef verdict PASS --agent worker\n", encoding="utf-8"
    )
    spec = importlib.util.spec_from_file_location("coord_write_check_ok", STATIC_CHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.TARGETS = (tmp_path,)
    assert module.main() == 0


def test_generated_agent_guidance_contains_coord_only_boundary(tmp_path: Path) -> None:
    ensure_pi_setup(home=tmp_path, agent_name="worker")
    outputs = (
        render_agents_block(agent_name="worker"),
        render_loader_script(),
        (tmp_path / "AGENTS.md").read_text(encoding="utf-8"),
        mandate_template("worker", "worker"),
        generate_soul_instructions(),
    )
    for output in outputs:
        assert "skcapstone coord" in output
        assert "Never create, append, rewrite, rename, or" in output or (
            "never mutate CardStore JSONL directly" in output
        )
    context_source = (ROOT / "src/skcapstone/context_loader.py").read_text(encoding="utf-8")
    assert "## Coordination Write Boundary" in context_source
    assert "delete CardStore JSONL" in context_source
