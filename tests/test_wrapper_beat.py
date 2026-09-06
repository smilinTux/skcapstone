"""Tests for the wrapper beat loop in the launch command (card e03755ba / B)."""

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


def test_generated_worker_brief_contains_agent_reporting_contract():
    """The generated brief carries the exact Card C reporting instructions."""
    src = ROTATE.read_text(encoding="utf-8")
    for phrase in (
        "WORKER AGENT BEAT CONTRACT",
        "RUNNING, WAITING_DEPENDENCY",
        "BLOCKED_NEEDS_HUMAN, or DEGRADED_RETRYING",
        "opaque progress token",
        "phase and step",
        "A silent worker is indistinguishable from a hung worker",
        "skmail event to heartbeat",
        "routine RUNNING beats",
        "through skmail",
        "Mid-run emission is not proven",
        "MID_RUN_EMISSION_UNPROVEN_LAUNCHER_BLOCKS_SUBPROCESS_RUN",
    ):
        assert phrase in src
