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
            audit_tmux_servers=kw.pop("audit_tmux_servers", True),
            store=object(),
            runner=runner,
            **kw,
        )

    return SimpleNamespace(**locals())


@pytest.mark.parametrize("audit", [True, False])
def test_stale_default_and_recovered_sockets_do_not_hide_audited_runtime(estate, audit):
    e = estate
    result = json.loads(e.publish(audit_tmux_servers=audit).read_text())
    assert result["complete"] is True
    assert result["tmux_sockets"] == [str(e.runtime)]
    assert result["tmux_sessions"] == ["skfleet-anchor"]
    assert not any(c[0] == "tmux" and c[2] != str(e.runtime) for c in e.calls)
    assert e.primary.exists() and e.stale.exists()


@pytest.mark.parametrize("audit", [True, False])
def test_missing_default_accepts_mapped_runtime_server(estate, audit):
    estate.primary.unlink()
    assert json.loads(estate.publish(audit_tmux_servers=audit).read_text())["tmux_sockets"] == [
        str(estate.runtime)
    ]


def test_stale_sockets_with_no_server_are_authoritative_absence(estate):
    estate.processes.clear()
    estate.listeners.clear()
    assert json.loads(estate.publish().read_text())["tmux_sockets"] == []


def test_audit_alone_skips_stale_socket_files(estate):
    names, paths = publisher._audited_tmux_sessions(estate.default, estate.runner)
    assert names == "skfleet-anchor" and paths == [str(estate.runtime)]


@pytest.mark.parametrize("failure", ["hidden", "extra-server", "live-probe", "process-probe"])
@pytest.mark.parametrize("audit", [True, False])
def test_incomplete_evidence_preserves_previous_snapshot(estate, failure, audit):
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
        e.publish(audit_tmux_servers=audit)
    assert target.read_text() == "previous"


def test_kernel_table_distinguishes_bound_listening_and_stale(tmp_path):
    path = tmp_path / "s"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
        assert path not in publisher._listening_unix_sockets()
        sock.listen(1)
        assert path in publisher._listening_unix_sockets()
    assert path.exists()
    assert path not in publisher._listening_unix_sockets()


@pytest.mark.parametrize(
    "table",
    [
        "",
        "wrong header\n",
        "Num RefCount Protocol Flags Type St Inode Path\nshort\n",
        "Num RefCount Protocol Flags Type St Inode Path\n0000: 2 0 bad-hex 0001 01 123 /socket\n",
        None,
    ],
)
def test_kernel_table_failure_preserves_previous_snapshot(estate, monkeypatch, table):
    e = estate
    target = e.tmp_path / "evidence/fleet-live/chiap08.json"
    target.parent.mkdir(parents=True)
    target.write_text("previous")
    original = Path.read_text

    def read(path, *args, **kw):
        if path == Path("/proc/net/unix"):
            if table is None:
                raise PermissionError("kernel table denied")
            return table
        return original(path, *args, **kw)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises((RuntimeError, PermissionError)):
        e.publish()
    assert target.read_text() == "previous"


def test_new_listener_during_audit_is_not_published_as_absent(estate):
    e = estate

    def runner(command, **kw):
        result = e.runner(command, **kw)
        if "display-message" in command:
            e.listeners.add(e.default)
        return result

    with pytest.raises(RuntimeError, match="listener inventory changed"):
        publisher._audited_tmux_sessions(e.default, runner)
