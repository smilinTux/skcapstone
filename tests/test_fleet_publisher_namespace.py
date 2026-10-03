"""Shared default namespace and exclusive authoritative-writer regressions."""

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone import fleet_live_publisher as publisher


@pytest.mark.parametrize(
    "processes,rc,success",
    [
        ("bash\npython3\n", 0, True),
        ("tmux: server\n", 0, False),
        ("", 1, False),
    ],
)
def test_default_absence_requires_successful_no_tmux_process_view(
    tmp_path, monkeypatch, processes, rc, success
):
    """No socket alone is never sufficient negative evidence."""
    monkeypatch.setattr(publisher.os, "getuid", lambda: 987654321)
    socket = "/tmp/tmux-987654321/default"
    seen = []

    def runner(command, **kwargs):
        seen.append(command)
        assert command[0] in ("ps", "systemctl")
        return SimpleNamespace(
            returncode=rc if command[0] == "ps" else 0,
            stdout=processes if command[0] == "ps" else "",
        )

    def call():
        return publisher.publish_host_snapshot(
            home=tmp_path,
            host="chiap01",
            tmux_socket=socket,
            allow_absent_default=True,
            runner=runner,
            store=object(),
        )

    if success:
        payload = json.loads(call().read_text())
        assert payload["complete"] is True and payload["cards"] == []
        assert payload["tmux_sessions"] == [] and payload["systemd_units"] == []
    else:
        with pytest.raises(RuntimeError):
            call()
        assert not (tmp_path / "evidence/fleet-live/chiap01.json").exists()


def test_custom_socket_absence_remains_unknown(tmp_path):
    """The verified default-only opt-in never launders a hidden custom socket."""
    with pytest.raises(RuntimeError):
        publisher.publish_host_snapshot(
            home=tmp_path,
            host="chiap01",
            tmux_socket=str(tmp_path / "absent"),
            allow_absent_default=True,
            store=object(),
        )


def test_rotation_capacity_write_preserves_authoritative_snapshot(tmp_path):
    """The legacy writer has no authority to replace a complete host report."""
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(script.read_text())
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("publish_live", "reporting_capacity")
    ]
    live = tmp_path / "fleet-live"
    live.mkdir()
    target = live / "chiap01.json"
    target.write_text('{"host":"chiap01","complete":true,"ts":100,"cards":["keep"]}')
    before = target.read_bytes()
    ns = {
        "os": os,
        "json": json,
        "Path": Path,
        "glob": __import__("glob"),
        "time": SimpleNamespace(time=lambda: 101),
        "LIVE": str(live),
        "HOST": "chiap01",
        "HOME": str(tmp_path),
        "LANES": [{"name": "codex", "target": 3, "busy": [], "free": 3}],
        "LIVE_FRESH": 1800,
        "_worker_cards": lambda *args: [],
        "CardStore": object,
        "log": lambda *args: None,
        "d": None,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(script), "exec"), ns)
    assert ns["publish_live"]([]) == []
    assert target.read_bytes() == before
    assert (
        json.loads((tmp_path / "fleet-lanes/chiap01.json").read_text())["lanes"]["codex"]["free"]
        == 3
    )
    assert ns["reporting_capacity"]()["chiap01"] == 3


@pytest.mark.parametrize(
    "before,after,accepted",
    [
        ([11], [11], True),
        ([11, 12], [11, 12], False),
        ([11], [11, 12], False),
    ],
)
def test_tmux_audit_requires_every_stable_server_to_be_reachable(
    tmp_path, monkeypatch, before, after, accepted
):
    """An orphaned or concurrently appearing server prevents authoritative emptiness."""
    import socket

    monkeypatch.setattr(publisher.os, "getuid", lambda: 987654321)
    primary = tmp_path / "default"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(primary))
    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        if command[0] == "ps":
            pids = before if calls == 0 else after
            calls += 1
            out = "".join(f"{pid} tmux: server\n" for pid in pids)
        elif "display-message" in command:
            out = "11\n"
        else:
            out = "codex-auto-aaaaaaaa\n"
        return SimpleNamespace(returncode=0, stdout=out)

    if accepted:
        names, paths = publisher._audited_tmux_sessions(primary, runner)
        assert names == "codex-auto-aaaaaaaa" and paths == [str(primary)]
    else:
        with pytest.raises(RuntimeError, match="coverage incomplete"):
            publisher._audited_tmux_sessions(primary, runner)


def test_tmux_audit_denied_directory_does_not_look_empty(tmp_path, monkeypatch):
    """Permission failure cannot be mistaken for a missing socket directory."""

    def denied(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(PermissionError):
        publisher._audited_tmux_sessions(
            tmp_path / "missing",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""),
        )


def test_lane_measurement_survives_dedicated_publication(tmp_path, monkeypatch):
    """The publisher carries the separate fresh lane measurement with its timestamp."""
    monkeypatch.setattr(publisher.os, "getuid", lambda: 987654321)
    lanes = tmp_path / "evidence/fleet-lanes/chiap01.json"
    lanes.parent.mkdir(parents=True)
    lanes.write_text(json.dumps({"ts": 100, "lanes": {"codex": {"free": 2}}}))
    target = publisher.publish_host_snapshot(
        home=tmp_path,
        host="chiap01",
        tmux_socket="/tmp/tmux-987654321/default",
        allow_absent_default=True,
        store=object(),
        now=lambda: 101,
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""),
    )
    snapshot = json.loads(target.read_text())
    assert snapshot["lanes"] == {"codex": {"free": 2}}
    assert snapshot["lanes_ts"] == 100
