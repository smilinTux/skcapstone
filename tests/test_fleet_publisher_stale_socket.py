"""Kernel listeners distinguish stale tmux files from hidden live servers."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone import fleet_live_publisher as publisher


@pytest.fixture
def estate(tmp_path, monkeypatch):
    root = tmp_path / "sockets"
    root.mkdir()
    uid = 987654321
    primary = root / "default"
    runtime = root / "tmux.sock"
    stale = root / "default.recovered-919444"
    for path in (primary, runtime, stale):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path))
    default = Path(f"/tmp/tmux-{uid}/default")
    original_stat, original_iterdir = Path.stat, Path.iterdir

    def stat(path, **kw):
        return original_stat(primary if path == default else path, **kw)

    def iterdir(path):
        if path == default.parent:
            return iter(([default] if primary.exists() else []) + [stale])
        if path == Path(f"/run/user/{uid}/skfleet"):
            return iter([runtime])
        return original_iterdir(path)

    monkeypatch.setattr(publisher.os, "getuid", lambda: uid)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    calls = []
    processes = [11]
    listeners = {runtime}
    original_read = Path.read_text

    def read_text(path, *args, **kw):
        if path == Path("/proc/net/unix"):
            return "Num RefCount Protocol Flags Type St Inode Path\n" + "".join(
                f"0000: 00000002 00000000 00010000 0001 01 123 {p}\n" for p in listeners
            )
        return original_read(path, *args, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)

    def runner(command, **kwargs):
        calls.append(command)
        if command[0] == "ps":
            out = "".join(f"{pid} tmux: server\n" for pid in processes)
        elif command[0] == "tmux":
            if command[2] != str(runtime):
                return SimpleNamespace(returncode=1, stdout="", stderr="failed to connect")
            out = "11\n" if "display-message" in command else "skfleet-anchor\n"
        else:
            out = ""
        return SimpleNamespace(returncode=0, stdout=out)

    def publish(**kw):
        return publisher.publish_host_snapshot(
            home=tmp_path,
            host="chiap08",
            tmux_socket=str(default),
            allow_absent_default=True,
            audit_tmux_servers=True,
            store=object(),
            runner=runner,
            **kw,
        )

    return SimpleNamespace(**locals())


def test_stale_default_and_recovered_sockets_do_not_hide_audited_runtime(estate):
    e = estate
    result = json.loads(e.publish().read_text())
    assert result["complete"] is True
    assert result["tmux_sockets"] == [str(e.runtime)]
    assert result["tmux_sessions"] == ["skfleet-anchor"]
    assert not any(c[0] == "tmux" and c[2] != str(e.runtime) for c in e.calls)
    assert e.primary.exists() and e.stale.exists()


def test_missing_default_accepts_mapped_runtime_server(estate):
    estate.primary.unlink()
    assert json.loads(estate.publish().read_text())["tmux_sockets"] == [str(estate.runtime)]


def test_stale_sockets_with_no_server_are_authoritative_absence(estate):
    estate.processes.clear()
    estate.listeners.clear()
    assert json.loads(estate.publish().read_text())["tmux_sockets"] == []


def test_audit_alone_skips_stale_socket_files(estate):
    names, paths = publisher._audited_tmux_sessions(estate.default, estate.runner)
    assert names == "skfleet-anchor" and paths == [str(estate.runtime)]


@pytest.mark.parametrize("failure", ["hidden", "extra-server", "live-probe", "process-probe"])
def test_incomplete_evidence_preserves_previous_snapshot(estate, failure):
    e = estate
    target = e.tmp_path / "evidence/fleet-live/chiap08.json"
    target.parent.mkdir(parents=True)
    target.write_text("previous")
    if failure == "hidden":
        e.primary.unlink()
        e.listeners.clear()
    elif failure == "extra-server":
        e.processes.append(12)
    elif failure == "live-probe":
        e.listeners.add(e.default)
    else:
        e.processes[:] = ["invalid"]
    with pytest.raises(RuntimeError):
        e.publish()
    assert target.read_text() == "previous"
