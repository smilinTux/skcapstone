"""Exercise host-local startup evidence against real child processes."""

import argparse
import ast
import datetime
import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from skcapstone.fleet.worker_watchdog import (
    DEFAULT_HEARTBEAT_TIMEOUT_S,
    HOST_LOCAL_BEAT_NOTICE_S,
    StartupObservation,
    startup_actuation_fenced,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "host",
        "claim",
        "unit",
        "session",
        "populated",
        "probe",
        "socket-missing",
        "socket-denied",
        "socket-exists",
    ],
)
def test_next_cycle_release_requires_negative_proof_and_fresh_fence(tmp_path, monkeypatch, fault):
    tree = ast.parse((ROOT / "scripts/fleet/skfleet-rotate.py").read_text())
    names = {"_startup_release_ready", "_release_failed_startups", "_worker_unit_name"}
    nodes = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    import re

    calls = []
    claim = ["worker", 123, "rev-1"]
    group = "/user.slice/skfleet-worker-codex-feedbeef.service"
    report = dict(
        host="other" if fault == "host" else "host-1",
        card_id="feedbeef",
        owner="worker",
        claim_revision="rev-1",
        attempt_id="attempt-1",
        session_id="session-1",
        lane="codex",
        state="startup-heartbeat-missing",
        control_group=group,
        child_pid=2147483647,
    )
    if fault == "claim":
        claim[2] = "newer"

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "systemctl":
            if fault == "probe":
                raise subprocess.TimeoutExpired(command, 5)
            active = "active" if fault == "unit" else "inactive"
            return subprocess.CompletedProcess(
                command,
                0,
                f"LoadState=loaded\nActiveState={active}\nMainPID=0\nControlPID=0\n",
                "",
            )
        if command[0] == "tmux":
            if fault in {"socket-missing", "socket-denied", "socket-exists"}:
                socket = tmp_path / "tmux-socket"
                if fault == "socket-exists":
                    socket.touch()
                reason = (
                    "Permission denied"
                    if fault == "socket-denied"
                    else "No such file or directory"
                )
                return subprocess.CompletedProcess(
                    command, 1, "", f"error connecting to {socket} ({reason})\n"
                )
            return subprocess.CompletedProcess(
                command, 0, "session-1\n" if fault == "session" else "", ""
            )
        assert command == [
            "skcapstone",
            "coord",
            "release-claim",
            "feedbeef",
            "--owner",
            "worker",
            "--expected-claim-revision",
            "rev-1",
            "--agent",
            "jarvis",
        ]
        claim[:] = [None, None, None]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/sys/fs/cgroup" + group + "/cgroup.events":
            return "populated 1\n" if fault == "populated" else "populated 0\n"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    namespace = dict(
        HOST="host-1",
        DRY=False,
        SKC="skcapstone",
        Path=Path,
        subprocess=subprocess,
        json=json,
        re=re,
        datetime=datetime,
        StartupObservation=StartupObservation,
        startup_actuation_fenced=startup_actuation_fenced,
        _current_claim_identity_fresh=lambda cid: tuple(claim),
        _WORKER_EXIT_DIR=str(tmp_path / "worker-exits"),
        d="unused",
        log=lambda *args: None,
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "startup-release", "exec"), namespace)
    directory = tmp_path / "worker-startup"
    directory.mkdir()
    (directory / "report.json").write_text(json.dumps(report))
    namespace["_release_failed_startups"]()
    release_calls = [call for call in calls if call[0] == "skcapstone"]
    assert len(release_calls) == (1 if fault in {None, "socket-missing"} else 0)


def wrapper():
    spec = importlib.util.spec_from_file_location(
        "startup_wrapper", ROOT / "scripts/fleet/skfleet-worker-wrapper.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "fault", [None, "heartbeat", "session", "attempt", "executable", "attribution", "node"]
)
def test_real_child_startup_requires_matching_proofs(tmp_path, monkeypatch, fault):
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "emit_work_mail", lambda *args: None)
    args = argparse.Namespace(
        owner="worker",
        card="feedbeef",
        session="session-1",
        claim_revision="rev-1",
        attempt_id="attempt-1",
        host="host-1",
        lane="codex",
        started_at=int(time.time()),
        worker_executable=sys.executable,
        startup_timeout=0.05,
        evidence_dir=tmp_path / "evidence/worker-exits",
    )
    env = {
        **os.environ,
        "SKAGENT": args.owner,
        "SKFLEET_CARD_ID": args.card,
        "SKFLEET_SESSION_ID": args.session,
        "SKFLEET_CLAIM_REVISION": args.claim_revision,
        "SKFLEET_ATTEMPT_ID": args.attempt_id,
    }
    if fault == "attribution":
        env["SKFLEET_CLAIM_REVISION"] = "older-revision"
    beat = {
        "owner": args.owner,
        "card_id": args.card,
        "session_id": args.session,
        "claim_revision": args.claim_revision,
        "attempt_id": args.attempt_id,
        "beat_at": args.started_at,
    }
    if fault == "session":
        beat["session_id"] = "previous-session"
    if fault == "attempt":
        beat["attempt_id"] = "previous-attempt"
    if fault != "heartbeat":
        path = tmp_path / ".skcapstone/fleet/beats/worker.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(beat))
    if fault == "executable":
        args.worker_executable = "/bin/false"
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    if fault == "node":
        node = shutil.which("node")
        if not node:
            pytest.skip("Node required for Pi shebang regression")
        script = tmp_path / "worker.js"
        script.write_text(f"#!{node}\nsetTimeout(() => {{}}, 10000);\n")
        script.chmod(0o700)
        link = tmp_path / "pi"
        link.symlink_to(script)
        args.worker_executable = str(link)
        args.startup_timeout = 2.0
        command = [str(link)]
    child = subprocess.Popen(command, env=env)
    try:
        module.monitor_startup(args, child, threading.Event())
        (record,) = (tmp_path / "evidence/worker-startup").glob("*.json")
        result = json.loads(record.read_text())
        assert (result["state"] == "startup-ready") is (fault in {None, "node"})
        assert result["release_recommended"] is False
        assert child.poll() is None
        if fault in {None, "node"}:
            assert result["executable_evidence"]["pid"] == child.pid
            assert result["executable_evidence"]["session_id"] == args.session
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("preflight", [0, 2])
def test_wrapper_reports_early_child_exit_without_waiting_for_deadline(
    tmp_path, monkeypatch, preflight
):
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "emit_work_mail", lambda *args: None)
    monkeypatch.setattr(module, "preflight_worktree", lambda: preflight)
    monkeypatch.setattr(
        module,
        "validate_cardstore_completion",
        lambda _args: (True, "valid_cardstore_completion", "PASS_FOR_REVIEW"),
    )
    args = argparse.Namespace(
        owner="worker",
        card="feedbeef",
        session="session-1",
        claim_revision="rev-1",
        host="host-1",
        lane="codex",
        model="fake",
        stdout=tmp_path / "stdout.log",
        worker_executable=sys.executable,
        startup_timeout=120.0,
        command=[sys.executable, "-c", "pass"],
        evidence_dir=tmp_path / "evidence/worker-exits",
    )
    monkeypatch.setattr(module, "parse_args", lambda: args)
    started = time.monotonic()
    assert module.main() == preflight
    assert time.monotonic() - started < 5.0
    (record,) = (tmp_path / "evidence/worker-startup").glob("*.json")
    result = json.loads(record.read_text())
    assert result["state"] == (
        "startup-preflight-blocked" if preflight else "startup-heartbeat-missing"
    )
    assert result["release_recommended"] is False
    if preflight:
        assert result["child_pid"] == os.getpid()
        assert result["heartbeat_at"] is None
        assert result["executable_evidence"] is None
        assert not args.stdout.exists()


def test_default_heartbeat_has_margin_below_notice_and_timeout(monkeypatch):
    monkeypatch.delenv("SKFLEET_BEAT_INTERVAL", raising=False)
    tree = ast.parse((ROOT / "scripts/fleet/skfleet-rotate.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_beat_interval"
    )
    namespace = {"os": os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "beat-interval", "exec"), namespace)
    interval = float(namespace["_beat_interval"]())
    assert interval == 60
    assert interval <= HOST_LOCAL_BEAT_NOTICE_S / 2
    assert interval <= DEFAULT_HEARTBEAT_TIMEOUT_S / 10


@pytest.mark.parametrize("override", [None, "17"])
def test_actual_launcher_shell_preserves_workspace_and_beat_identity(
    tmp_path, monkeypatch, override
):
    monkeypatch.delenv("SKFLEET_BEAT_INTERVAL", raising=False)
    if override is not None:
        monkeypatch.setenv("SKFLEET_BEAT_INTERVAL", override)
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    tree = ast.parse(source)
    interval_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_beat_interval"
    )
    interval_assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_bi" for target in node.targets)
    )
    assignment = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "child" for target in node.targets)
    )
    workspace = tmp_path / "work space"
    workspace.mkdir()
    pi = tmp_path / "fake pi"
    pi.write_text("#!/bin/bash\nprintf '%s' \"$SKFLEET_WORKSPACE\"\nsleep 0.1\n")
    pi.chmod(0o700)
    brief = tmp_path / "brief"
    brief.write_text("synthetic")
    beat = tmp_path / "beat.json"
    skc = tmp_path / "fake-skc"
    skc.write_text(
        f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
        "Path(os.environ['FENCE_ARGS']).write_text(json.dumps(sys.argv[1:]))\n"
    )
    skc.chmod(0o700)
    fence_args = tmp_path / "fence-args.json"
    namespace = dict(
        SKC=str(skc),
        cid="feedbeef",
        name="worker",
        claimed_revision="rev-1",
        sess="session-1",
        _bf_path=str(beat),
        os=os,
        workspace=str(workspace),
        PI=str(pi),
        model="fake",
        pi_tools="bash",
        bf=str(brief),
        shlex=shlex,
    )
    exec(
        compile(
            ast.Module(body=[interval_function, interval_assignment, assignment], type_ignores=[]),
            "launcher",
            "exec",
        ),
        namespace,
    )
    assert namespace["_bi"] == (override or "60")
    assert f"sleep {override or '60'} & wait $!" in namespace["child"]
    result = subprocess.run(
        ["bash", "-c", namespace["child"]],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "HOME": str(tmp_path), "FENCE_ARGS": str(fence_args)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(workspace)
    payload = json.loads(beat.read_text())
    assert payload["session_id"] == "session-1"
    assert payload["claim_revision"] == "rev-1"
    assert json.loads(fence_args.read_text()) == [
        "coord",
        "release-claim",
        "feedbeef",
        "--owner",
        "worker",
        "--expected-claim-revision",
        "rev-1",
        "--agent",
        "worker",
    ]


def test_wrapper_completion_reaps_long_heartbeat_sleeper_and_closes_pipes(tmp_path):
    """A finished Pi child cannot leave a 600-second sleeper holding stderr."""
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    assignment = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "child" for target in node.targets)
    )
    sleeper_pids = tmp_path / "sleeper-pids"
    sleep = tmp_path / "sleep"
    sleep.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$BASHPID" >> "$SLEEP_PID_FILE"\nexec /bin/sleep "$@"\n'
    )
    sleep.chmod(0o700)
    pi = tmp_path / "pi"
    pi.write_text(
        '#!/bin/bash\nwhile [ ! -s "$SLEEP_PID_FILE" ]; do /bin/sleep 0.01; done\n'
        'printf "PASS_FOR_REVIEW\\n"\n'
    )
    pi.chmod(0o700)
    brief = tmp_path / "brief"
    brief.write_text("synthetic")
    namespace = dict(
        SKC="/bin/true",
        cid="feedbeef",
        name="worker",
        claimed_revision="rev-1",
        sess="session-1",
        _bf_path=str(tmp_path / "beat.json"),
        _bi=600,
        workspace=str(tmp_path),
        PI=str(pi),
        model="fake",
        pi_tools="bash",
        bf=str(brief),
        shlex=shlex,
    )
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), "launcher", "exec"), namespace)
    wrapper_path = ROOT / "scripts/fleet/skfleet-worker-wrapper.py"
    # Run the real wrapper in a disposable process group. External notices and
    # worktree preflight are unrelated to pipe and heartbeat shutdown semantics.
    harness = (
        "import importlib.util,sys; "
        "s=importlib.util.spec_from_file_location('wrapper',sys.argv.pop(1)); "
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
        "m.emit_work_mail=lambda *a:None;m.preflight_worktree=lambda:0; "
        "m.validate_cardstore_completion=lambda a:"
        "(True,'valid_cardstore_completion','PASS_FOR_REVIEW'); "
        "raise SystemExit(m.main())"
    )
    command = [
        sys.executable,
        "-c",
        harness,
        str(wrapper_path),
        "--card",
        "feedbeef",
        "--owner",
        "worker",
        "--claim-revision",
        "rev-1",
        "--host",
        "host-1",
        "--lane",
        "codex",
        "--model",
        "fake",
        "--stdout",
        str(tmp_path / "worker.log"),
        "--evidence-dir",
        str(tmp_path / "exits"),
        "--",
        "bash",
        "-c",
        namespace["child"],
    ]
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "SLEEP_PID_FILE": str(sleeper_pids),
        "PYTHONPATH": str(ROOT / "src"),
    }
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True
    )
    try:
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr.decode()
        assert time.monotonic() - started < 5
        assert (tmp_path / "worker.log").read_text() == "PASS_FOR_REVIEW\n"
        pids = [int(value) for value in sleeper_pids.read_text().split()]
        assert pids
        assert all(not Path("/proc", str(pid)).exists() for pid in pids)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)
