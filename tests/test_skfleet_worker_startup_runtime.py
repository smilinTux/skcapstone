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
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet.worker_watchdog import (
    DEFAULT_HEARTBEAT_TIMEOUT_S,
    HOST_LOCAL_BEAT_NOTICE_S,
    StartupObservation,
    startup_actuation_fenced,
)
from skcapstone.seat_mail import MailPoll

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


@pytest.mark.parametrize("host", ["chiap01", "chiap02", "chiap03", "chiap04", "chiap08"])
@pytest.mark.parametrize("lane", ["codex", "glm", "qwen", "kimi", "escalate"])
def test_mailbox_canary_is_one_hello_and_one_direct_plus_all_poll(
    tmp_path, monkeypatch, host, lane
):
    module = wrapper()
    calls = []
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        module,
        "startup_hello",
        lambda home, owner, host=None: calls.append(("hello", owner, host)) or True,
    )
    monkeypatch.setattr(
        module,
        "poll_mail",
        lambda owner: (
            calls.append(("poll", owner))
            or MailPoll("now", True, new_messages=2, help_or_handoff=1, digest="a" * 64)
        ),
    )
    args = argparse.Namespace(owner="worker", host=host, lane=lane)
    assert module.preflight_mailbox(args) is True
    assert calls == [("hello", "worker", host), ("poll", "worker")]
    assert args.mailbox_poll["help_or_handoff"] == 1


@pytest.mark.parametrize(
    "fault", [None, "heartbeat", "session", "executable", "attribution", "node"]
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
    }
    if fault == "attribution":
        env["SKFLEET_CLAIM_REVISION"] = "older-revision"
    beat = {
        "owner": args.owner,
        "card_id": args.card,
        "session_id": args.session,
        "claim_revision": args.claim_revision,
        "beat_at": args.started_at,
    }
    if fault == "session":
        beat["session_id"] = "previous-session"
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
        script.write_text(f"#!{node}\nprocess.title = 'pi'; setTimeout(() => {{}}, 10000);\n")
        script.chmod(0o700)
        link = tmp_path / "pi"
        link.symlink_to(script)
        args.worker_executable = str(link)
        args.startup_timeout = 2.0
        command = [str(link)]
    child = subprocess.Popen(command, env=env)
    try:
        if fault == "node":
            time.sleep(0.1)
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
    monkeypatch.setattr(module, "preflight_mailbox", lambda args: True)
    args = argparse.Namespace(
        owner="worker",
        card="feedbeef",
        session="session-1",
        claim_revision="rev-1",
        host="host-1",
        lane="codex",
        model="fake",
        stdout=tmp_path / "stdout.log",
        live_snapshot=None,
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
        "startup-preflight-blocked" if preflight else "startup-child-exited"
    )
    assert result["release_recommended"] is False
    if preflight:
        assert result["child_pid"] == os.getpid()
        assert result["heartbeat_at"] is None
        assert result["executable_evidence"] is None
        assert not args.stdout.exists()


def test_wrapper_fails_closed_before_work_when_mailbox_is_unavailable(tmp_path, monkeypatch):
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    monkeypatch.setattr(module, "preflight_mailbox", lambda args: False)
    monkeypatch.setattr(module, "emit_work_mail", lambda *args: None)
    args = argparse.Namespace(
        owner="worker",
        card="feedbeef",
        session="session-1",
        claim_revision="rev-1",
        host="chiap01",
        lane="codex",
        model="fake",
        stdout=tmp_path / "work-never-started.log",
        live_snapshot=None,
        worker_executable=sys.executable,
        startup_timeout=1.0,
        command=[sys.executable, "-c", "raise SystemExit('must not run')"],
        evidence_dir=tmp_path / "evidence/worker-exits",
    )
    monkeypatch.setattr(module, "parse_args", lambda: args)
    assert module.main() == 2
    assert not args.stdout.exists()
    (record,) = (tmp_path / "evidence/worker-startup").glob("*.json")
    assert json.loads(record.read_text())["state"] == "startup-mailbox-unavailable"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_wrapper_publishes_terminal_capacity_on_every_child_exit(tmp_path, monkeypatch, exit_code):
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "emit_work_mail", lambda *args: None)
    monkeypatch.setattr(module, "idle_owner_projection", lambda *args: None)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    monkeypatch.setattr(module, "preflight_mailbox", lambda args: True)
    monkeypatch.setattr(
        module,
        "terminal_local_evidence",
        lambda child: child is not None and child.poll() is not None,
    )
    monkeypatch.setattr(module, "retire_worker_generation", lambda *args: {"cards": ["sibling"]})
    snapshot = tmp_path / "fleet-live.json"
    snapshot.write_text(json.dumps({"cards": ["feedbeef", "sibling"]}))
    args = argparse.Namespace(
        owner="worker",
        card="feedbeef",
        session="",
        claim_revision="rev-1",
        host="host-1",
        lane="codex",
        model="fake",
        stdout=tmp_path / "stdout.log",
        live_snapshot=snapshot,
        worker_executable="",
        startup_timeout=120.0,
        command=[sys.executable, "-c", f"raise SystemExit({exit_code})"],
        evidence_dir=tmp_path / "evidence/worker-exits",
    )
    monkeypatch.setattr(module, "parse_args", lambda: args)

    assert module.main() == exit_code
    assert module.publish_terminal_capacity(args, None) is False


def test_wrapper_keeps_ambiguous_live_child_occupied(tmp_path, monkeypatch):
    module = wrapper()
    snapshot = tmp_path / "fleet-live.json"
    snapshot.write_text(json.dumps({"cards": ["feedbeef", "sibling"]}))
    args = argparse.Namespace(
        card="feedbeef",
        owner="worker",
        claim_revision="rev-1",
        host="host-1",
        live_snapshot=snapshot,
    )

    class LiveChild:
        @staticmethod
        def poll():
            return None

    assert module.publish_terminal_capacity(args, LiveChild()) is False
    assert json.loads(snapshot.read_text())["cards"] == ["feedbeef", "sibling"]


def test_wrapper_keeps_capacity_when_no_child_was_started(tmp_path):
    module = wrapper()
    snapshot = tmp_path / "fleet-live.json"
    snapshot.write_text(json.dumps({"cards": ["feedbeef"]}))
    args = argparse.Namespace(
        card="feedbeef",
        owner="worker",
        claim_revision="rev-1",
        host="host-1",
        live_snapshot=snapshot,
    )

    assert module.publish_terminal_capacity(args, None) is False
    assert json.loads(snapshot.read_text())["cards"] == ["feedbeef"]


def test_terminal_wrapper_exit_allows_real_next_claim_and_managed_launch(tmp_path, monkeypatch):
    module = wrapper()
    home = tmp_path / ".skcapstone"
    home.mkdir()
    store = CardStore(home)
    store.create(
        CardCore(
            id="feedbeef",
            title="first worker",
            initial_owner="worker-1",
            initial_claim_revision="rev-1",
        )
    )
    snapshot = tmp_path / "fleet-live.json"
    snapshot.write_text(
        json.dumps(
            {
                "host": "host-1",
                "cards": ["feedbeef"],
                "workers": [
                    {"card_id": "feedbeef", "owner": "worker-1", "claim_revision": "rev-1"}
                ],
                "lanes": {"codex": {"busy": 1, "free": 0, "target": 1}},
            }
        )
    )
    args = argparse.Namespace(
        owner="worker-1",
        card="feedbeef",
        session="",
        claim_revision="rev-1",
        host="host-1",
        lane="codex",
        model="fake",
        stdout=tmp_path / "first.log",
        live_snapshot=snapshot,
        worker_executable="",
        startup_timeout=120.0,
        command=[sys.executable, "-c", "print('first-terminal')"],
        evidence_dir=tmp_path / "exits",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(module, "parse_args", lambda: args)
    monkeypatch.setattr(module, "emit_work_mail", lambda *args: None)
    monkeypatch.setattr(module, "idle_owner_projection", lambda *args: None)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    monkeypatch.setattr(module, "preflight_mailbox", lambda args: True)
    monkeypatch.setattr(module, "terminal_local_evidence", lambda child: child.poll() is not None)

    assert module.main() == 0
    assert store.fold("feedbeef").owner is None
    assert json.loads(snapshot.read_text())["cards"] == []

    cli = shutil.which("skcapstone")
    assert cli is not None
    claim = subprocess.run(
        [cli, "coord", "claim", "feedbeef", "--agent", "worker-2", "--home", str(home)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert claim.returncode == 0, claim.stderr

    rotate_tree = ast.parse((ROOT / "scripts/fleet/skfleet-rotate.py").read_text())
    launch_node = next(
        node
        for node in rotate_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_worker_launch_command"
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[launch_node], type_ignores=[]), "managed-launch", "exec"),
        namespace,
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemd_run = fake_bin / "systemd-run"
    systemd_run.write_text(
        f"#!{sys.executable}\n"
        "import subprocess,sys\n"
        "args=sys.argv[1:]\n"
        "i=args.index('--working-directory')\n"
        "raise SystemExit(subprocess.run(args[i+2:],cwd=args[i+1]).returncode)\n"
    )
    systemd_run.chmod(0o700)
    marker = tmp_path / "second-launched"
    command = namespace["_worker_launch_command"](
        "skfleet-worker-codex-feedbeef",
        str(tmp_path),
        [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"],
    )
    launched = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
    )
    assert launched.returncode == 0, launched.stderr
    assert marker.exists()
    assert store.fold("feedbeef").owner == "worker-2"


@pytest.mark.parametrize("fault", [None, "child", "peer", "cgroup", "malformed"])
def test_terminal_local_evidence_reconciles_process_tree_and_cgroup(tmp_path, monkeypatch, fault):
    module = wrapper()
    proc = tmp_path / "proc"
    cgroups = tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (cgroups / "worker.slice").mkdir(parents=True)
    (proc / "self/cgroup").write_text(
        "malformed\n" if fault == "malformed" else "0::/worker.slice\n"
    )
    members = [os.getpid()]
    if fault == "peer":
        members.append(2147483646)
    (cgroups / "worker.slice/cgroup.procs").write_text(
        "\n".join(str(member) for member in members)
    )

    class Child:
        pid = 2147483647

        @staticmethod
        def poll():
            return None if fault == "child" else 0

    if fault == "child":
        (proc / str(Child.pid)).touch()
    if fault == "cgroup":
        (cgroups / "worker.slice/cgroup.procs").unlink()

    assert module.terminal_local_evidence(Child(), proc_root=proc, cgroup_root=cgroups) is (
        fault is None
    )


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
    namespace = dict(
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
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(workspace)
    payload = json.loads(beat.read_text())
    assert payload["session_id"] == "session-1"
    assert payload["claim_revision"] == "rev-1"
    assert "release-claim" not in namespace["child"]


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
        "m.preflight_mailbox=lambda a:True; "
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
