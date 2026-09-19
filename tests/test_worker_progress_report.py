"""Call-site and behaviour tests for the worker progress measurement pass.

The pass was report-only from 2026-09-18 until the measurement window closed
on 2026-09-19. The REPORTER is still report-only and these tests still hold
it to that: measurement and actuation are separate functions on purpose, so
that the thing which decides cannot quietly grow the power to act. The
actuator is ``_reap_wedged_workers`` and is covered in test_wedge_reaper.py.


Contract 1 of docs/fleet/2026-09-18-learnings.md: a function can be correct
and unreachable at the same time. ``classify_progress`` had a passing unit
test for its whole life while nothing called it, so these tests assert the
CALL SITE in scripts/fleet/skfleet-rotate.py, not just the function.

The behaviour tests lift the shipped source out of the script with ast (the
same pattern as tests/test_reaper_stall.py), so they test the code that runs
rather than a paraphrase of it.
"""

from __future__ import annotations

import ast
import datetime
import glob
import json
import os
import time
from pathlib import Path

from skcapstone.fleet.worker_watchdog import (
    DEFAULT_PROGRESS_TIMEOUT_S,
    DEFAULT_WEDGE_TIMEOUT_S,
    ProgressObservation,
    classify_progress,
    classify_wedge,
)

SRC = Path(__file__).resolve().parent.parent / "scripts" / "fleet" / "skfleet-rotate.py"


def _tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert found, f"{name} is not defined at module level in {SRC.name}"
    return found[0]


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            target = child.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_classify_progress_has_a_dispatcher_call_site():
    """The watchdog classifier must be CALLED by the worker-health pass."""
    tree = _tree()
    reporter = _function(tree, "_report_worker_progress")
    called = _called_names(reporter)
    assert "classify_progress" in called
    assert "ProgressObservation" in called


def test_health_pass_calls_the_progress_reporter():
    """reap_dead_claims (the pass that logs WORKER_HEALTH) runs the reporter."""
    tree = _tree()
    health_pass = _function(tree, "reap_dead_claims")
    assert "_report_worker_progress" in _called_names(health_pass)


def test_progress_reporter_is_report_only():
    """The reporter must not actuate: no subprocess, no release, no reap."""
    tree = _tree()
    reporter = _function(tree, "_report_worker_progress")
    called = _called_names(reporter)
    forbidden = {
        "run",
        "sh",
        "Popen",
        "check_call",
        "check_output",
        "_record_reap_outcome",
        "_release_card_admission",
        "kill",
        "unlink",
        "remove",
    }
    overlap = called & forbidden
    assert not overlap, f"report-only pass performs actuation-shaped calls: {overlap}"


def _lift(names, extra_globals):
    """Execute the named module-level defs from the shipped script source."""
    tree = _tree()
    body = [
        node
        for node in tree.body
        if (isinstance(node, (ast.FunctionDef, ast.Assign)))
        and (
            (isinstance(node, ast.FunctionDef) and node.name in names)
            or (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in names)
        )
    ]
    lifted = {
        name
        for node in body
        for name in ([node.name] if isinstance(node, ast.FunctionDef) else [node.targets[0].id])
    }
    missing = set(names) - lifted
    assert not missing, f"not defined at module level in {SRC.name}: {missing}"
    namespace = dict(extra_globals)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SRC), "exec"), namespace)
    return namespace


def _reporter_namespace(tmp_path, lines):
    receipt_dir = tmp_path / ".skcapstone" / "fleet" / "admission"
    receipt_dir.mkdir(parents=True)
    workspace = tmp_path / ".skcapstone" / "fleet" / "workspaces" / "pi-codex-test-cafe0001"
    workspace.mkdir(parents=True)
    (receipt_dir / "cafe0001.json").write_text(
        json.dumps(
            {
                "host": "testhost",
                "session": "codex-auto-cafe0001",
                "owner": "pi-codex-test-cafe0001",
                "claim_revision": "rev-1",
                "workspace": str(workspace),
            }
        ),
        encoding="utf-8",
    )
    namespace = _lift(
        [
            "_workspace_progress_at",
            "_session_progress_at",
            "_report_worker_progress",
            "_admission_lock_path",
            "_read_admission_receipt",
            "_PROGRESS_SCAN_CAP",
            "_PROGRESS_FRESH_EXIT_S",
            "_PROGRESS_IGNORED_DIRS",
            "_SESSION_ROOT",
        ],
        {
            "os": os,
            "glob": glob,
            "json": json,
            "time": time,
            "datetime": datetime,
            "LANES": [{"name": "codex", "prefix": "codex-auto-"}],
            "HOME": str(tmp_path),
            "HOST": "testhost",
            "CARDS": str(tmp_path / ".skcapstone" / "cards"),
            "d": None,
            "log": lambda _d, message: lines.append(message),
            "seat_for": lambda cid, core: None,
            "_worker_owner": lambda lane, cid, seat=None: f"pi-{seat or lane}-testhost-{cid}",
            "_current_claim_identity_fresh": lambda cid: ("pi-codex-test-cafe0001", 1.0, "rev-1"),
            "classify_progress": classify_progress,
            "classify_wedge": classify_wedge,
            "ProgressObservation": ProgressObservation,
            "DEFAULT_PROGRESS_TIMEOUT_S": DEFAULT_PROGRESS_TIMEOUT_S,
            "DEFAULT_WEDGE_TIMEOUT_S": DEFAULT_WEDGE_TIMEOUT_S,
            "_wedge_mode": lambda *_a, **_k: "",
        },
    )
    return namespace, workspace


def test_stale_workspace_reports_progress_stale(tmp_path):
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    old = time.time() - 2 * 3600
    marker = workspace / "output.txt"
    marker.write_text("work", encoding="utf-8")
    os.utime(marker, (old, old))
    os.utime(workspace, (old, old))
    namespace["_report_worker_progress"](["codex-auto-cafe0001", "unrelated-session"])
    progress = [line for line in lines if line.startswith("WORKER_PROGRESS|")]
    assert len(progress) == 1, lines
    assert "state=progress-stale" in progress[0]
    # This assertion used to read `actuation=report-only`, which encoded the
    # deliberate measurement window opened on 2026-09-18. That window has now
    # closed; the measurement is recorded in
    # docs/fleet/wedged-worker-actuation.md and the line reports the live
    # rollout mode instead. `off` is still the default, so the shipped
    # behaviour on an unconfigured host is unchanged: the pass observes and
    # nothing acts.
    assert "actuation=off" in progress[0]
    # A two-hour-old write is progress-stale and is emphatically NOT wedged.
    # With no transcript to read, the reading is workspace mtime, and a
    # workspace-mtime reading is never actuated at all: productive workers
    # were measured going up to 29,181s between file writes, so the stale and
    # working populations overlap and no threshold on it separates them.
    assert "wedge=wedge-unmeasured" in progress[0]
    assert "source=workspace-mtime" in progress[0]
    assert "|codex-auto-cafe0001|cafe0001|" in progress[0]
    assert "owner=pi-codex-test-cafe0001" in progress[0]


def test_fresh_workspace_reports_progress_fresh(tmp_path):
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    (workspace / "output.txt").write_text("work", encoding="utf-8")
    namespace["_report_worker_progress"](["codex-auto-cafe0001"])
    progress = [line for line in lines if line.startswith("WORKER_PROGRESS|")]
    assert len(progress) == 1, lines
    assert "state=progress-fresh" in progress[0]


def test_missing_workspace_reports_progress_missing(tmp_path):
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    for stale in sorted(workspace.rglob("*"), reverse=True):
        stale.unlink()
    workspace.rmdir()
    namespace["_report_worker_progress"](["codex-auto-cafe0001"])
    progress = [line for line in lines if line.startswith("WORKER_PROGRESS|")]
    assert len(progress) == 1, lines
    assert "state=progress-missing" in progress[0]


def test_unit_workers_are_reported_without_tmux_sessions(tmp_path):
    """Production workers are systemd transient units; tmux is migration-era.

    Measured on chi 2026-09-18: both live workers (9e15f83c on chiap02,
    abe011e9 on chiap04) ran only as skfleet-worker-*.service units, with
    zero lane tmux sessions on either host. A tmux-only enumeration would
    report nothing while the whole fleet works, which is the exact
    invisibility this pass exists to end.
    """
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    (workspace / "output.txt").write_text("work", encoding="utf-8")
    namespace["_report_worker_progress"](
        [],
        [{"unit": "skfleet-worker-codex-cafe0001.service", "lane": "codex", "card": "cafe0001"}],
    )
    progress = [line for line in lines if line.startswith("WORKER_PROGRESS|")]
    assert len(progress) == 1, lines
    assert "state=progress-fresh" in progress[0]
    assert "|codex-auto-cafe0001|cafe0001|" in progress[0]


def test_health_pass_feeds_units_to_the_reporter():
    """The call site passes systemd units, not only tmux sessions."""
    tree = _tree()
    health_pass = _function(tree, "reap_dead_claims")
    calls = [
        child
        for child in ast.walk(health_pass)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "_report_worker_progress"
    ]
    assert calls, "reap_dead_claims does not call _report_worker_progress"
    argument_names = {
        name.id
        for call in calls
        for argument in call.args
        for name in ast.walk(argument)
        if isinstance(name, ast.Name)
    } | {
        name.id
        for call in calls
        for argument in call.args
        for name in ast.walk(argument)
        if isinstance(name, ast.Call) and isinstance(name.func, ast.Name)
        for name in [name.func]
    }
    assert "active_worker_units" in argument_names, (
        "the reporter call site must enumerate systemd worker units; " f"saw only {argument_names}"
    )


def test_workspace_scan_is_bounded(tmp_path):
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    old = time.time() - 2 * 3600
    for index in range(20):
        stale = workspace / f"file-{index}"
        stale.write_text("x", encoding="utf-8")
        os.utime(stale, (old, old))
    os.utime(workspace, (old, old))
    newest, scanned, truncated = namespace["_workspace_progress_at"](str(workspace), cap=5)
    assert truncated is True
    assert scanned <= 5


# ---------------------------------------------------------------------------
# The transcript oracle (2026-09-19).
#
# Workspace mtime could not tell a reading worker from a stopped one, and it
# was additionally saturated by directory mtimes: `git status` writes no file
# but still bumps `.git`, so six chi workers with zero edits, zero commits and
# zero dirty files across 2.5h to 6h every one reported progress_age_s=11..26
# and classified wedge-progressing. The reaper could never fire.
# ---------------------------------------------------------------------------


def _session_file(tmp_path, workspace, age_s):
    """Write a pi transcript for this workspace, aged age_s seconds."""
    slug = tmp_path / ".pi" / "agent" / "sessions" / f"--slugged-{workspace.name}--"
    slug.mkdir(parents=True, exist_ok=True)
    path = slug / "2026-09-19T08-00-00-000Z_01a0b8c3.jsonl"
    path.write_text('{"type":"message"}\n', encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_a_directory_mtime_is_not_progress(tmp_path):
    """The `.git` bug: a transient file created and unlinked bumps the dir.

    Every wedged chi worker on 2026-09-19 had a `.git` directory 30-60s old
    while every file inside it, `.git/index` included, was hours old. Nothing
    had been written; ordinary git reads move the directory.
    """
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    old = time.time() - 3 * 3600
    marker = workspace / "output.txt"
    marker.write_text("work", encoding="utf-8")
    os.utime(marker, (old, old))
    git = workspace / ".git"
    git.mkdir()
    index = git / "index"
    index.write_text("i", encoding="utf-8")
    os.utime(index, (old, old))
    os.utime(workspace, (old, old))
    # .git itself is fresh, exactly as a `git status` leaves it.
    newest, _scanned, _truncated = namespace["_workspace_progress_at"](str(workspace))
    assert newest is not None
    assert (
        time.time() - newest > 3600
    ), "a fresh directory mtime was counted as a write; that is the bug"


def test_tool_caches_are_not_progress(tmp_path):
    """A worker that only re-runs pytest/ruff/uv is not making progress."""
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    old = time.time() - 3 * 3600
    marker = workspace / "output.txt"
    marker.write_text("work", encoding="utf-8")
    os.utime(marker, (old, old))
    for cache in (".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"):
        noisy = workspace / cache / "v"
        noisy.mkdir(parents=True)
        (noisy / "nodeids").write_text("fresh", encoding="utf-8")
    os.utime(workspace, (old, old))
    newest, _scanned, _truncated = namespace["_workspace_progress_at"](str(workspace))
    assert newest is not None
    assert time.time() - newest > 3600


def test_a_live_transcript_beats_a_stale_workspace(tmp_path):
    """The six-worker case: reading and reasoning is not being stopped.

    This is the assertion that protects a worker doing hours of read-only
    investigation. Its workspace has not been touched since checkout; its
    transcript is seconds old; it must read fresh and must never be a wedge
    candidate.
    """
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    old = time.time() - 6 * 3600
    marker = workspace / "output.txt"
    marker.write_text("work", encoding="utf-8")
    os.utime(marker, (old, old))
    os.utime(workspace, (old, old))
    _session_file(tmp_path, workspace, age_s=5)
    namespace["_report_worker_progress"](["codex-auto-cafe0001"])
    progress = [line for line in lines if line.startswith("WORKER_PROGRESS|")]
    assert len(progress) == 1, lines
    assert "source=session-mtime" in progress[0]
    assert "state=progress-fresh" in progress[0]
    assert "wedge=wedge-progressing" in progress[0]


def test_a_silent_transcript_past_the_deadline_is_a_wedge(tmp_path):
    """The 139ec63d shape: alive, holding the claim, transcript frozen."""
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    (workspace / "output.txt").write_text("work", encoding="utf-8")
    _session_file(tmp_path, workspace, age_s=DEFAULT_WEDGE_TIMEOUT_S + 600)
    namespace["_report_worker_progress"](["codex-auto-cafe0001"])
    progress = [line for line in lines if line.startswith("WORKER_PROGRESS|")]
    assert len(progress) == 1, lines
    assert "source=session-mtime" in progress[0]
    assert "wedge=wedge-stale-confirmed" in progress[0]


def test_a_silent_transcript_inside_the_deadline_is_never_a_wedge(tmp_path):
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    _session_file(tmp_path, workspace, age_s=DEFAULT_WEDGE_TIMEOUT_S - 600)
    namespace["_report_worker_progress"](["codex-auto-cafe0001"])
    progress = [line for line in lines if line.startswith("WORKER_PROGRESS|")]
    assert "wedge=wedge-within-margin" in progress[0]


def test_only_the_transcript_may_actuate(tmp_path):
    """No transcript, no kill. Workspace mtime reports and never acts."""
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    old = time.time() - 30 * 3600
    marker = workspace / "output.txt"
    marker.write_text("work", encoding="utf-8")
    os.utime(marker, (old, old))
    os.utime(workspace, (old, old))
    namespace["_report_worker_progress"](["codex-auto-cafe0001"])
    progress = [line for line in lines if line.startswith("WORKER_PROGRESS|")]
    assert "source=workspace-mtime" in progress[0]
    # 30h stale, far past any deadline, and still refused.
    assert "wedge=wedge-unmeasured" in progress[0]


def test_the_newest_transcript_wins_when_a_worker_has_several(tmp_path):
    lines = []
    namespace, workspace = _reporter_namespace(tmp_path, lines)
    slug = tmp_path / ".pi" / "agent" / "sessions" / f"--slugged-{workspace.name}--"
    slug.mkdir(parents=True)
    for index, age in enumerate((40000.0, 20.0, 9000.0)):
        path = slug / f"session-{index}.jsonl"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (time.time() - age, time.time() - age))
    newest, seen = namespace["_session_progress_at"](
        str(workspace), root=str(tmp_path / ".pi" / "agent" / "sessions")
    )
    assert seen == 3
    assert time.time() - newest < 60


def test_the_deadline_clears_the_longest_legal_tool_call():
    """A single bash call may hold the transcript silent for its timeout.

    The largest timeout any chi worker has ever issued is 3600s, a full
    pytest run. The deadline must sit above it by construction, or a worker
    running the test suite is a kill candidate.
    """
    assert DEFAULT_WEDGE_TIMEOUT_S > 3600.0


def test_the_deadline_clears_the_worst_observed_healthy_silence():
    """2,558s, measured over 2,754 fleet sessions on 2026-09-19."""
    assert DEFAULT_WEDGE_TIMEOUT_S > 2558.0


def test_the_deadline_stays_below_the_known_incident():
    """139ec63d sat silent 22,680s. The deadline must fire well before that."""
    assert DEFAULT_WEDGE_TIMEOUT_S < 22680.0
