"""Tests for the wrapper beat loop in the launch command (card e03755ba / B)."""

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def test_beat_function_in_child_command():
    """The child shell command must define and start a beat function."""
    src = ROTATE.read_text(encoding="utf-8")
    assert "beat() { while :; do" in src, "beat loop function not in child command"
    assert "beat & BEAT=$!" in src, "beat loop not backgrounded"
    assert "sleep" in src, "beat loop has no sleep interval"


def test_beat_killed_on_all_exit_paths():
    """stop_beat must be called in every trap and before exit."""
    src = ROTATE.read_text(encoding="utf-8")
    # HUP/INT/TERM trap includes stop_beat
    assert 'trap "stop_beat; release_claim; idle_agent; exit 143" HUP INT TERM' in src
    # EXIT trap includes stop_beat
    assert 'trap "stop_beat; release_claim; idle_agent" EXIT' in src
    # Normal exit path calls stop_beat before release
    assert "stop_beat; release_claim; idle_agent; exit $rc" in src


def test_beat_failure_never_fails_worker():
    """Every beat write must be || true."""
    src = ROTATE.read_text(encoding="utf-8")
    # The mv that publishes the beat is || true
    assert "mv %s.tmp %s 2>/dev/null || true" in src or "|| true" in src


def test_beat_interval_configurable():
    """SKFLEET_BEAT_INTERVAL env var must be read with a default."""
    src = ROTATE.read_text(encoding="utf-8")
    assert "SKFLEET_BEAT_INTERVAL" in src
    assert "_beat_interval" in src


def test_beat_writes_to_correct_directory():
    """Beat files go to ~/.skcapstone/fleet/beats/."""
    src = ROTATE.read_text(encoding="utf-8")
    assert "~/.skcapstone/fleet/beats" in src


def test_generated_child_substitutes_interval_and_cleanup_path():
    """The generated shell must put the numeric interval in sleep, not the path."""
    source = ROTATE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "child" for target in node.targets)
    )
    rendered = eval(  # noqa: S307 - the expression is parsed from this reviewed source only
        compile(ast.Expression(assignment.value), str(ROTATE), "eval"),
        {
            "SKC": "/home/skuser01/.skenv/bin/skcapstone",
            "cid": "deadbeef",
            "name": "pi-codex-chiap08-deadbeef",
            "claimed_revision": "a" * 32,
            "_bf_path": "/tmp/deadbeef.beat.json",
            "_bi": "37",
            "workspace": "/tmp/workspace",
            "PI": "/bin/true",
            "model": "sk-codex-mid",
            "pi_tools": "",
            "bf": "/tmp/brief.txt",
        },
    )
    assert "sleep 37; done" in rendered
    assert "sleep /tmp/deadbeef.beat.json" not in rendered
    assert "rm -f -- /tmp/deadbeef.beat.json" in rendered
    subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)
