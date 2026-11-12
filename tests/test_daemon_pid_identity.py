"""A PID number alone must never authorize daemon lifecycle operations."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from unittest.mock import patch

import pytest

from skcapstone import daemon_pid as subject


def test_unrelated_live_pid_is_not_a_daemon(tmp_path):
    (tmp_path / "daemon.pid").write_text(str(os.getpid()))
    with patch.object(subject.signal, "pidfd_send_signal") as send:
        assert subject.stop_daemon(tmp_path) is None
        send.assert_not_called()
    assert not (tmp_path / "daemon.pid").exists()


def test_record_round_trip_and_other_home_refusal(tmp_path):
    subject.write_daemon_pid(tmp_path)
    assert subject.read_daemon_pid(tmp_path) == os.getpid()
    record_path = tmp_path / "daemon.pid.identity.json"
    record = json.loads(record_path.read_text())
    record["home"] = str(tmp_path / "other")
    record_path.write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="disagree"):
        subject.read_daemon_pid(tmp_path)
    assert (tmp_path / "daemon.pid").exists()


@pytest.mark.parametrize("field,value", [("boot_id", "other-boot"), ("start_ticks", -1)])
def test_recycled_process_generation_is_not_signaled(tmp_path, field, value):
    subject.write_daemon_pid(tmp_path)
    record_path = tmp_path / "daemon.pid.identity.json"
    record = json.loads(record_path.read_text())
    record[field] = value
    record_path.write_text(json.dumps(record))
    with patch.object(subject.signal, "pidfd_send_signal") as send:
        assert subject.stop_daemon(tmp_path) is None
        send.assert_not_called()


def test_unreadable_legacy_identity_refuses_without_clearing_pid(tmp_path):
    (tmp_path / "daemon.pid").write_text(str(os.getpid()))
    with patch.object(subject, "_legacy_home", side_effect=PermissionError):
        with pytest.raises(RuntimeError, match="ownership"):
            subject.read_daemon_pid(tmp_path)
    assert (tmp_path / "daemon.pid").exists()


def test_live_legacy_daemon_with_old_metadata_blocks_duplicate_start(tmp_path):
    subject.write_daemon_pid(tmp_path)
    path = tmp_path / "daemon.pid.identity.json"
    record = json.loads(path.read_text())
    record["boot_id"] = "old-boot"
    path.write_text(json.dumps(record))
    with patch.object(subject, "_legacy_home", return_value=tmp_path):
        with pytest.raises(RuntimeError, match="conflicts"):
            subject.read_daemon_pid(tmp_path)
    assert (tmp_path / "daemon.pid").exists()


def test_legacy_other_agent_is_never_stopped(tmp_path):
    (tmp_path / "daemon.pid").write_text(str(os.getpid()))
    with (
        patch.object(subject, "_legacy_home", return_value=tmp_path / "other"),
        patch.object(subject.signal, "pidfd_send_signal") as send,
    ):
        assert subject.stop_daemon(tmp_path) is None
        send.assert_not_called()


def test_pid_replacement_during_read_is_not_deleted(tmp_path):
    path = tmp_path / "daemon.pid"
    path.write_text(str(os.getpid()))

    def replace(_pid):
        newer = tmp_path / "new-pid"
        newer.write_text("12345")
        newer.replace(path)
        return None

    with patch.object(subject, "_legacy_home", side_effect=replace):
        with pytest.raises(RuntimeError, match="changed"):
            subject.read_daemon_pid(tmp_path)
    assert path.read_text() == "12345"


def test_recycle_between_pidfd_open_and_signal_is_refused(tmp_path):
    subject.write_daemon_pid(tmp_path)
    generation = subject._generation(os.getpid())
    changed = subject.ProcessGeneration(
        generation.pid, generation.boot_id, generation.start_ticks + 1
    )
    with (
        patch.object(subject, "_generation", side_effect=[generation, generation, changed]),
        patch.object(subject.os, "pidfd_open", return_value=999),
        patch.object(subject.os, "close") as close,
        patch.object(subject.signal, "pidfd_send_signal") as send,
    ):
        with pytest.raises(RuntimeError, match="changed"):
            subject.stop_daemon(tmp_path)
        send.assert_not_called()
        close.assert_called_once_with(999)


def test_pidfd_stops_only_the_recorded_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        generation = subject._generation(child.pid)
        (tmp_path / "daemon.pid").write_text(str(child.pid))
        (tmp_path / "daemon.pid.identity.json").write_text(
            json.dumps({**asdict(generation), "home": str(tmp_path)})
        )
        assert subject.stop_daemon(tmp_path) == child.pid
        assert child.wait(timeout=5) == -15
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_legacy_named_and_explicit_home_cli(tmp_path):
    script = tmp_path / "skcapstone"
    script.write_text("import time; print('ready', flush=True); time.sleep(60)")
    for args, expected in [
        (["--home", str(tmp_path)], tmp_path),
        (["--agent", "jarvis"], tmp_path / ".skcapstone/agents/jarvis"),
    ]:
        child = subprocess.Popen(
            [sys.executable, str(script), "daemon", "start", *args],
            stdout=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "HOME": str(tmp_path),
                "SKCAPSTONE_HOME": str(tmp_path / ".skcapstone"),
            },
        )
        try:
            assert child.stdout.readline().strip() == "ready"
            assert subject._legacy_home(child.pid) == expected.resolve()
        finally:
            child.terminate()
            child.wait()
