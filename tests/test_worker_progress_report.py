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
            "_report_worker_progress",
            "_admission_lock_path",
            "_read_admission_receipt",
            "_PROGRESS_SCAN_CAP",
            "_PROGRESS_FRESH_EXIT_S",
        ],
        {
            "os": os,
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
    # 900s is where a worker stops looking fresh; 14400s is where it may be
    # ended. The gap between them is the entire safety margin.
    assert "wedge=wedge-within-margin" in progress[0]
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
