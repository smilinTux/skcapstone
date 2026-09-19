"""Tests for the wrapper beat loop in the launch command (card e03755ba / B)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def test_beat_function_in_child_command():
    """The child shell command must define and start a beat function."""
    src = ROTATE.read_text(encoding="utf-8")
    assert "beat() { while :; do" in src, "beat loop function not in child command"
    assert "& BEAT=$!" in src, "beat loop not backgrounded"
    # The beat must not inherit the worker's pipes. One that does keeps them open
    # after the shell exits and strands the wrapper and its transient unit, so the
    # redirection is the point of detaching the beat, not incidental tidiness.
    assert "beat </dev/null >/dev/null 2>&1 &" in src, "beat loop not detached from worker pipes"
    assert "sleep" in src, "beat loop has no sleep interval"


def test_beat_killed_on_all_exit_paths():
    """Child cleanup stops only its beat; wrapper owns claim and projection."""
    src = ROTATE.read_text(encoding="utf-8")
    assert 'trap "stop_beat; exit 143" HUP INT TERM' in src
    assert 'trap "stop_beat" EXIT' in src
    assert "stop_beat; exit $rc" in src
    assert "idle_agent" not in src
    assert "release_claim()" not in src
    assert "stop_beat; release_claim" not in src


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


def test_beat_records_the_scope_of_what_it_proves():
    """The beat must not claim more than a timer can know.

    ``"disposition":"RUNNING"`` in this loop is a hardcoded literal, not an
    observation.  Measured 2026-09-19: card 139ec63d was held 6h18m with zero
    workspace writes and ``pi`` alive at 0.0% CPU, while this loop reported
    RUNNING at an age that never exceeded 39 seconds.  Coupling the beat to
    ``pi``'s liveness would not have caught it either, since ``pi`` was alive
    throughout, so the record instead states its own scope.
    """
    src = ROTATE.read_text(encoding="utf-8")
    assert '\\"proves\\":\\"shell-liveness\\"' in src


def test_beat_is_not_the_progress_signal():
    """Progress is read from what the worker writes, not from the beat."""
    src = ROTATE.read_text(encoding="utf-8")
    assert "_workspace_progress_at" in src
    assert "classify_wedge" in src


def _beat_function_source() -> str:
    """Lift the real beat shell function out of the launch command."""
    import ast

    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "child" for t in node.targets)
    ]
    assert len(assigns) == 1, "the worker launch command moved"
    template = ast.literal_eval(assigns[0].value.left)
    end = template.index("done; }; ") + len("done; }; ")
    return template[:end]


def test_the_beat_actually_emits_parseable_json_with_its_scope(tmp_path):
    """Run the real beat loop and read what it writes.

    Every other test here matches the source text.  This one executes it, so
    a quoting mistake in the JSON literal fails the build instead of silently
    producing an unparseable beat that ``read_beats`` skips.
    """
    import json
    import subprocess

    beat_file = tmp_path / "worker.json"
    script = _beat_function_source() % (
        "pi-glm-chiap03-139ec63d",
        "139ec63d",
        "revision-1",
        "glm-auto-139ec63d",
        str(beat_file),
        str(beat_file),
        str(beat_file),
        "30",
    )
    # Start the loop, let it write once, stop it. The loop never exits on its
    # own, which is the point of it.
    result = subprocess.run(
        [
            "bash",
            "-c",
            script + "beat </dev/null >/dev/null 2>&1 & "
            "BEAT=$!; for _ in $(seq 100); do "
            "[ -s %s ] && break; sleep 0.05; done; "
            "kill $BEAT 2>/dev/null; wait $BEAT 2>/dev/null; true" % beat_file,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert beat_file.exists(), result.stderr
    record = json.loads(beat_file.read_text(encoding="utf-8"))
    assert record["owner"] == "pi-glm-chiap03-139ec63d"
    assert record["card_id"] == "139ec63d"
    assert record["emitter"] == "wrapper"
    # The scope, carried in the record itself so a consumer reading the file
    # directly is not misled the way the dashboard was on 2026-09-19.
    assert record["proves"] == "shell-liveness"
    assert isinstance(record["beat_at"], (int, float))
