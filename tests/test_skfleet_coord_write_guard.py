"""Fleet workers use the mediated coordination write surface."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import click
import pytest
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


def _compaction_events(tmp_path: Path, scenario: str) -> dict:
    """Exercise supported extension events without a provider or live board."""
    script = f"""
import guard from {json.dumps(GUARD.as_uri())};
const hooks = new Map(), entries = [], signals = [];
process.kill = (_pid, signal) => signals.push(signal);
const pi = {{
  on(name, callback) {{ hooks.set(name, callback); }},
  appendEntry(customType, data) {{ entries.push({{ type: "custom", customType, data }}); }}
}};
guard(pi);
const ctx = {{ cwd: process.env.SKFLEET_WORKSPACE, abort() {{}},
  sessionManager: {{ getEntries: () => entries, getSessionId: () => "session-1",
    getSessionFile: () => "/private/session.jsonl" }} }};
async function emit(name, event) {{
  const hook = hooks.get(name);
  if (!hook) throw new Error(`Missing required event: ${{name}}`);
  await Promise.race([hook(event, ctx), new Promise(resolve => setTimeout(resolve, 10))]);
}}
async function compact(id, reason = "threshold") {{
  const entry = {{ type: "compaction", id, summary: "Measured state, pending review." }};
  entries.push(entry);
  await emit("session_compact", {{ compactionEntry: entry, reason,
    willRetry: reason === "overflow" }});
}}
{scenario}
const blocked = await hooks.get("tool_call")({{
  toolName: "bash", input: {{ command: "skcapstone coord link deadbeef verdict PASS" }}
}}, ctx);
console.log(JSON.stringify({{ signals, blocked: blocked ?? null, entries }}));
process.exit(0);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "SKFLEET_CARD_ID": "deadbeef",
            "SKFLEET_CLAIM_REVISION": "claim-123",
            "SKAGENT": "worker-test",
            "SKFLEET_WORKSPACE": str(tmp_path),
        },
    )
    return json.loads(result.stdout)


def test_first_auto_compaction_continues_and_manual_does_not_spend_limit(tmp_path) -> None:
    result = _compaction_events(
        tmp_path,
        'await compact("manual", "manual"); await compact("first");',
    )
    assert result["signals"] == []
    assert result["blocked"] is None
    assert not list(tmp_path.rglob(".handoff*.md"))


@pytest.mark.parametrize("reason", ["threshold", "overflow"])
def test_second_auto_compaction_hands_off_and_blocks_success(tmp_path, reason) -> None:
    result = _compaction_events(
        tmp_path, f'await compact("one"); await compact("two", "{reason}");'
    )
    assert result["signals"] == ["SIGTERM"]
    assert result["blocked"]["block"] is True
    assert result["blocked"]["terminate"] is True
    handoff = tmp_path / ".skcapstone/evidence/work/deadbeef/.handoff.md"
    body = handoff.read_text()
    assert all(value in body for value in ("deadbeef", "claim-123", "session-1", "worker-test"))
    assert "Measured state, pending review." in body
    assert handoff.stat().st_mode & 0o777 == 0o600
    assert handoff.parent.stat().st_mode & 0o777 == 0o700


def test_resume_counts_historical_compactions_and_stops_before_work(tmp_path) -> None:
    result = _compaction_events(
        tmp_path,
        'entries.push({type:"compaction",id:"a"},{type:"compaction",id:"b"}); '
        'await emit("session_start", {reason:"resume"});',
    )
    assert result["signals"] == ["SIGTERM"]
    assert result["blocked"]["terminate"] is True


def test_resume_keeps_manual_exclusion_but_counts_prior_auto(tmp_path) -> None:
    result = _compaction_events(
        tmp_path,
        'await compact("manual", "manual"); await compact("first"); '
        "hooks.clear(); guard(pi); "
        'await emit("session_start", {reason:"resume"}); await compact("second");',
    )
    assert result["signals"] == ["SIGTERM"]


def test_reason_write_failure_stops_and_blocks_all_tools(tmp_path) -> None:
    result = _compaction_events(
        tmp_path,
        'pi.appendEntry = () => {throw new Error("storage unavailable");}; '
        'await compact("first"); '
        'for (const toolName of ["write", "edit", "read", "bash"]) { '
        'const r = await hooks.get("tool_call")({toolName, input:{}}, ctx); '
        'if (!r?.block || !r?.terminate) throw new Error("tool escaped limit"); }',
    )
    assert result["signals"] == ["SIGTERM"]


def test_manual_only_and_failed_compactions_do_not_stop(tmp_path) -> None:
    result = _compaction_events(
        tmp_path,
        'await compact("manual-one", "manual"); await compact("manual-two", "manual"); '
        'await hooks.get("session_compact_failed")?.({reason:"overflow", aborted:true}, ctx); '
        'await emit("session_start", {reason:"resume"});',
    )
    assert result["signals"] == []
    assert result["blocked"] is None


def test_duplicate_summary_event_uses_latest_saved_compaction(tmp_path) -> None:
    result = _compaction_events(
        tmp_path,
        'await compact("manual-one", "manual"); await compact("first"); '
        'entries.push({type:"compaction", id:"second", summary:"same summary"}); '
        'await emit("session_compact", {reason:"threshold", compactionEntry:entries[0]});',
    )
    assert result["signals"] == ["SIGTERM"]


def test_real_signal_cleanup_runs_before_any_continuation(tmp_path) -> None:
    """A pending Promise alone can exit Node before its SIGTERM handler runs."""
    script = f"""
import guard from {json.dumps(GUARD.as_uri())};
const hooks = new Map();
const entries = [{{type:"compaction",id:"one"}}, {{type:"compaction",id:"two"}}];
guard({{on(name, hook) {{ hooks.set(name, hook); }} }});
process.on("SIGTERM", () => {{ console.log("cleanup"); process.exit(143); }});
const ctx = {{cwd: process.cwd(), sessionManager:{{getEntries:()=>entries,
 getSessionId:()=>"test-session",getSessionFile:()=>"private-session"}} }};
await hooks.get("session_start")({{reason:"resume"}}, ctx);
console.log("unexpected continuation");
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "HOME": str(tmp_path), "SKFLEET_CARD_ID": "deadbeef"},
    )
    assert result.returncode == 143, result.stderr
    assert result.stdout == "cleanup\n"


def test_handoff_preserves_existing_work(tmp_path) -> None:
    folder = tmp_path / ".skcapstone/evidence/work/deadbeef"
    folder.mkdir(parents=True, mode=0o700)
    existing = folder / ".handoff.md"
    existing.write_text("prior work")
    existing.chmod(0o600)
    result = _compaction_events(tmp_path, 'await compact("one"); await compact("two");')
    assert result["signals"] == ["SIGTERM"]
    assert existing.read_text() == "prior work"
    assert len(list(folder.glob(".handoff*.md"))) == 2


@pytest.mark.parametrize("unsafe", ["symlink", "public-directory"])
def test_handoff_write_failure_still_stops_without_following_symlink(tmp_path, unsafe) -> None:
    folder = tmp_path / ".skcapstone/evidence/work/deadbeef"
    folder.parent.mkdir(parents=True)
    if unsafe == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        folder.symlink_to(elsewhere, target_is_directory=True)
    else:
        folder.mkdir(mode=0o755)
    result = _compaction_events(tmp_path, 'await compact("one"); await compact("two");')
    assert result["signals"] == ["SIGTERM"]
    assert result["blocked"]["terminate"] is True
    assert not list(tmp_path.rglob(".handoff*.md"))


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
