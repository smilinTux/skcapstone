"""Tests for the host-local, read-only, fail-closed fleet liveness publisher."""

from __future__ import annotations

import json
import socket as socket_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone import fleet_live_publisher
from skcapstone.fleet_live_publisher import publish_host_snapshot


class _Store:
    """Return fixed exact claim generations for test workers."""

    def fold(self, card_id: str) -> object:
        if card_id == "aaaaaaaa":
            return SimpleNamespace(owner="codex-host-aaaaaaaa", meta={"_claim_revision": "rev-a"})
        return {"owner": "glm-host-bbbbbbbb", "meta": {"_claim_revision": "rev-b"}}


def _real_socket(tmp_path: Path, name: str = "tmux.sock") -> Path:
    """Materialize a real unix socket file without a listening server."""
    socket_path = tmp_path / name
    listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
    finally:
        listener.close()
    return socket_path


def _unit_runner(sessions: str = "") -> object:
    """Build a runner that serves tmux sessions and empty running units."""

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "tmux":
            return SimpleNamespace(returncode=0, stdout=sessions, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return runner


def test_publisher_reads_panes_through_the_explicit_socket(tmp_path: Path) -> None:
    """The happy path publishes both worker runtimes with exact generations."""
    socket_path = _real_socket(tmp_path)
    seen: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        seen.append(command)
        if command[0] == "tmux":
            return SimpleNamespace(returncode=0, stdout="codex-auto-aaaaaaaa\nnoise\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="skfleet-worker-glm-bbbbbbbb.service loaded active running worker\n",
            stderr="",
        )

    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap01",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=runner,
        now=lambda: 1234.5,
    )

    assert seen[0][:3] == ["tmux", "-S", str(socket_path)]
    assert target == tmp_path / "evidence" / "fleet-live" / "chiap01.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "cards": ["aaaaaaaa", "bbbbbbbb"],
        "host": "chiap01",
        "lanes": "unknown",
        "tmux_socket": str(socket_path),
        "ts": 1234.5,
        "workers": [
            {"card_id": "aaaaaaaa", "claim_revision": "rev-a", "owner": "codex-host-aaaaaaaa"},
            {"card_id": "bbbbbbbb", "claim_revision": "rev-b", "owner": "glm-host-bbbbbbbb"},
        ],
    }


def test_explicit_socket_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packaged unit contract works without a command-line override."""
    socket_path = _real_socket(tmp_path)
    monkeypatch.setenv("SKFLEET_TMUX_SOCKET", str(socket_path))

    target = publish_host_snapshot(
        home=tmp_path, host="chiap01", store=_Store(), runner=_unit_runner()
    )

    assert json.loads(target.read_text(encoding="utf-8"))["tmux_socket"] == str(socket_path)


def test_missing_explicit_socket_fails_closed_and_preserves_evidence(tmp_path: Path) -> None:
    """A socket the namespace cannot see never publishes a false empty view."""
    target = tmp_path / "evidence" / "fleet-live" / "chiap01.json"
    target.parent.mkdir(parents=True)
    target.write_text("previous\n", encoding="utf-8")
    missing = tmp_path / "absent.sock"

    with pytest.raises(RuntimeError, match="not present"):
        publish_host_snapshot(
            home=tmp_path,
            host="chiap01",
            tmux_socket=str(missing),
            store=_Store(),
            runner=_unit_runner(),
        )

    assert target.read_text(encoding="utf-8") == "previous\n"
    assert list(target.parent.iterdir()) == [target]


def test_non_socket_path_fails_closed_and_preserves_evidence(tmp_path: Path) -> None:
    """A regular file is rejected so tmux cannot spawn a false server."""
    fake = tmp_path / "not-a-socket"
    fake.write_text("impostor\n", encoding="utf-8")
    target = tmp_path / "evidence" / "fleet-live" / "chiap01.json"
    target.parent.mkdir(parents=True)
    target.write_text("previous\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a socket"):
        publish_host_snapshot(
            home=tmp_path,
            host="chiap01",
            tmux_socket=str(fake),
            store=_Store(),
            runner=_unit_runner(),
        )

    assert target.read_text(encoding="utf-8") == "previous\n"


def test_failed_to_connect_is_never_empty_success(tmp_path: Path) -> None:
    """The PrivateTmp-hidden-socket signature now fails closed, snapshot intact."""
    socket_path = _real_socket(tmp_path)
    target = tmp_path / "evidence" / "fleet-live" / "chiap01.json"
    target.parent.mkdir(parents=True)
    target.write_text("previous\n", encoding="utf-8")

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "tmux":
            return SimpleNamespace(returncode=1, stdout="", stderr="failed to connect to server")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeError, match="tmux liveness probe failed"):
        publish_host_snapshot(
            home=tmp_path,
            host="chiap01",
            tmux_socket=str(socket_path),
            store=_Store(),
            runner=runner,
        )

    assert target.read_text(encoding="utf-8") == "previous\n"


@pytest.mark.parametrize("failed_probe", ["tmux", "systemctl"])
def test_publisher_fails_closed_when_a_local_process_probe_fails(
    tmp_path: Path, failed_probe: str
) -> None:
    """An incomplete local view never overwrites the last trustworthy snapshot."""
    socket_path = _real_socket(tmp_path)
    target = tmp_path / "evidence" / "fleet-live" / "chiap01.json"
    target.parent.mkdir(parents=True)
    target.write_text("previous\n", encoding="utf-8")

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        failed = command[0] == failed_probe
        return SimpleNamespace(returncode=1 if failed else 0, stdout="", stderr="failed")

    with pytest.raises(RuntimeError, match=f"{failed_probe} liveness probe failed"):
        publish_host_snapshot(
            home=tmp_path,
            host="chiap01",
            tmux_socket=str(socket_path),
            store=_Store(),
            runner=runner,
        )

    assert target.read_text(encoding="utf-8") == "previous\n"
    assert list(target.parent.iterdir()) == [target]


def test_zero_sessions_on_a_live_explicit_socket_is_truthful(tmp_path: Path) -> None:
    """A reachable server with zero sessions is authoritative emptiness."""
    socket_path = _real_socket(tmp_path)

    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap01",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=_unit_runner(),
        now=lambda: 7.0,
    )

    assert json.loads(target.read_text(encoding="utf-8"))["cards"] == []


def test_publication_is_atomic_for_concurrent_readers(tmp_path: Path) -> None:
    """Readers only ever observe the complete previous or complete new payload."""
    socket_path = _real_socket(tmp_path)
    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap01",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=_unit_runner("codex-auto-aaaaaaaa\n"),
        now=lambda: 1.0,
    )
    before = target.read_bytes()

    publish_host_snapshot(
        home=tmp_path,
        host="chiap01",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=_unit_runner("codex-auto-aaaaaaaa\nglm-auto-bbbbbbbb\n"),
        now=lambda: 2.0,
    )

    after = json.loads(target.read_bytes())
    assert json.loads(before)["cards"] == ["aaaaaaaa"]
    assert after["cards"] == ["aaaaaaaa", "bbbbbbbb"]
    assert after["ts"] == 2.0
    assert sorted(p.name for p in target.parent.iterdir()) == ["chiap01.json"]


def test_main_requires_an_explicit_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without any configured socket the CLI exits nonzero without publishing."""
    monkeypatch.delenv("SKFLEET_TMUX_SOCKET", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["fleet_live_publisher", "--home", str(tmp_path), "--host", "chiap01"],
    )

    assert fleet_live_publisher.main() == 1
    assert "explicit tmux socket required" in capsys.readouterr().err
    assert not (tmp_path / "evidence").exists()


def test_main_fails_closed_without_replacing_the_last_good_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI failure preserves prior evidence and leaves no temporary files."""
    target = tmp_path / "evidence" / "fleet-live" / "chiap01.json"
    target.parent.mkdir(parents=True)
    target.write_text("previous\n", encoding="utf-8")
    monkeypatch.setenv("SKFLEET_TMUX_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setattr(
        "sys.argv",
        ["fleet_live_publisher", "--home", str(tmp_path), "--host", "chiap01"],
    )

    assert fleet_live_publisher.main() == 1
    assert "not present" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "previous\n"
    assert list(target.parent.iterdir()) == [target]


def test_publisher_has_no_dispatch_surface() -> None:
    """The standalone module cannot select, claim, release, or launch work."""
    source = (
        Path(__file__).parents[1] / "src" / "skcapstone" / "fleet_live_publisher.py"
    ).read_text(encoding="utf-8")

    for forbidden in ("coord claim", "release-claim", "skfleet-rotate", "--go", "Popen("):
        assert forbidden not in source


def test_service_preserves_the_explicit_namespace_safe_socket_contract() -> None:
    """The oneshot preserves its shared socket while retaining safety guards."""
    root = Path(__file__).parents[1]
    units = [
        (root / "systemd" / "skfleet-live-publisher.service").read_text(encoding="utf-8"),
        (
            root / "src" / "skcapstone" / "data" / "systemd" / "skfleet-live-publisher.service"
        ).read_text(encoding="utf-8"),
    ]
    assert units[0] == units[1]
    for service in units:
        assert "Type=oneshot" in service
        assert "PrivateTmp=yes" in service
        assert "Environment=SKFLEET_TMUX_SOCKET=%t/skfleet/tmux.sock" in service
        assert "RuntimeDirectory=skfleet" in service
        assert "RuntimeDirectoryPreserve=yes" in service
        assert "/tmp" not in service.split("ExecStart", 1)[0]
        assert "-m skcapstone.fleet_live_publisher" in service
        assert "skfleet-rotate" not in service
        assert "niobe_live_entrypoint" not in service


def test_packaged_timer_is_distinct_from_disabled_rotation_and_central_dispatch() -> None:
    """Publication gets its own unit and does not alter either dispatch scheduler."""
    root = Path(__file__).parents[1]
    for unit_root in (root / "systemd", root / "src" / "skcapstone" / "data" / "systemd"):
        timer = (unit_root / "skfleet-live-publisher.timer").read_text(encoding="utf-8")
        assert "Unit=skfleet-live-publisher.service" in timer

    rollout = (root / "docs" / "fleet" / "liveness-publisher-rollout.md").read_text()
    assert "skfleet-rotate.timer remains disabled" in rollout
    assert "skfleet-niobe-live.timer remains the sole centralized dispatcher" in rollout
    assert "Do not execute these commands on this\nsource-repair card" in rollout


# ---------------------------------------------------------------------------
# Lane capacity: carry the dispatcher's verified numbers, never invent zeros.
# The publisher used to hardcode `"lanes": {}`, which every consumer that sums
# `free` reads as "this host has zero capacity". Running 41 seconds after the
# dispatcher on the packaged timers, it clobbered the dispatcher's truthful
# lane table every cycle, so chiap03 advertised capacity 0 while its own SLOTS
# line said total_free=9.
# ---------------------------------------------------------------------------


def _dispatcher_snapshot(home: Path, host: str, ts: float, lanes: object) -> Path:
    """Seed the snapshot the dispatcher's publish_live would have written."""
    target = home / "evidence" / "fleet-live" / f"{host}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"host": host, "ts": ts, "cards": [], "workers": [], "lanes": lanes}),
        encoding="utf-8",
    )
    return target


_CHIAP03_LANES = {
    "codex": {"target": 7, "busy": 6, "free": 1},
    "glm": {"target": 3, "busy": 0, "free": 3},
    "kimi": {"target": 3, "busy": 0, "free": 3},
    "qwen": {"target": 0, "busy": 0, "free": 0},
    "escalate": {"target": 2, "busy": 0, "free": 2},
}


def test_snapshot_carries_dispatcher_lane_capacity(tmp_path: Path) -> None:
    """A fresh dispatcher lane table survives the publisher's refresh intact."""
    socket_path = _real_socket(tmp_path)
    _dispatcher_snapshot(tmp_path, "chiap03", 900.0, _CHIAP03_LANES)

    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap03",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=_unit_runner(),
        now=lambda: 1000.0,
    )

    snap = json.loads(target.read_text(encoding="utf-8"))
    assert snap["lanes"] == _CHIAP03_LANES
    assert sum(lane["free"] for lane in snap["lanes"].values()) == 9
    assert snap["lanes_ts"] == 900.0


def test_unmeasured_capacity_never_reads_as_zero_capacity(tmp_path: Path) -> None:
    """No lane source at publish time is 'unknown', never an empty (zero) map."""
    socket_path = _real_socket(tmp_path)

    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap03",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=_unit_runner(),
        now=lambda: 1000.0,
    )

    snap = json.loads(target.read_text(encoding="utf-8"))
    assert snap["lanes"] == "unknown"
    assert not isinstance(snap["lanes"], dict)
    assert "lanes_ts" not in snap


def test_genuine_zero_capacity_is_distinct_from_unknown(tmp_path: Path) -> None:
    """A dispatcher-measured all-busy host publishes zeros, not 'unknown'."""
    socket_path = _real_socket(tmp_path)
    saturated = {"codex": {"target": 7, "busy": 7, "free": 0}}
    _dispatcher_snapshot(tmp_path, "chiap03", 990.0, saturated)

    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap03",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=_unit_runner(),
        now=lambda: 1000.0,
    )

    snap = json.loads(target.read_text(encoding="utf-8"))
    assert snap["lanes"] == saturated
    assert snap["lanes"] != "unknown"
    assert sum(lane["free"] for lane in snap["lanes"].values()) == 0


def test_stale_dispatcher_capacity_ages_out_to_unknown(tmp_path: Path) -> None:
    """Lane data older than the reader's freshness fence is not re-freshened."""
    socket_path = _real_socket(tmp_path)
    _dispatcher_snapshot(tmp_path, "chiap03", 900.0, _CHIAP03_LANES)

    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap03",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=_unit_runner(),
        now=lambda: 900.0 + 30 * 60 + 1,
    )

    assert json.loads(target.read_text(encoding="utf-8"))["lanes"] == "unknown"


def test_carried_capacity_keeps_original_measurement_time(tmp_path: Path) -> None:
    """Repeated publisher runs never launder old capacity into fresh capacity."""
    socket_path = _real_socket(tmp_path)
    _dispatcher_snapshot(tmp_path, "chiap03", 900.0, _CHIAP03_LANES)
    common = {
        "home": tmp_path,
        "host": "chiap03",
        "tmux_socket": str(socket_path),
        "store": _Store(),
        "runner": _unit_runner(),
    }

    publish_host_snapshot(now=lambda: 1000.0, **common)
    target = publish_host_snapshot(now=lambda: 1500.0, **common)
    snap = json.loads(target.read_text(encoding="utf-8"))
    assert snap["lanes"] == _CHIAP03_LANES
    assert snap["lanes_ts"] == 900.0

    target = publish_host_snapshot(now=lambda: 900.0 + 30 * 60 + 1, **common)
    snap = json.loads(target.read_text(encoding="utf-8"))
    assert snap["lanes"] == "unknown"
    assert "lanes_ts" not in snap


def test_legacy_empty_lane_map_is_treated_as_no_data(tmp_path: Path) -> None:
    """The old hardcoded `{}` carries no measurement and must not persist."""
    socket_path = _real_socket(tmp_path)
    _dispatcher_snapshot(tmp_path, "chiap03", 990.0, {})

    target = publish_host_snapshot(
        home=tmp_path,
        host="chiap03",
        tmux_socket=str(socket_path),
        store=_Store(),
        runner=_unit_runner(),
        now=lambda: 1000.0,
    )

    assert json.loads(target.read_text(encoding="utf-8"))["lanes"] == "unknown"
