"""Synced test-run launches are charged only on their own host, and not forever."""

import json
import os
import time
from types import SimpleNamespace

from skcapstone.fleet import production_resources as resources

RESOURCES = {"memory_max_bytes": 3 * 1024**3, "runtime_max_seconds": 3600}


def _launch(home, name, marker, age=0):
    run = home / "fleet/test-runs" / name
    run.mkdir(parents=True, mode=0o700)
    for parent in (run.parent, run.parent.parent):
        parent.chmod(0o700)
    argv = ["systemd-run", f"--setenv=SKFLEET_ADMISSION_ID={marker}", "--user"]
    path = run / "launch.json"
    path.touch(mode=0o600)
    path.write_text(
        json.dumps(
            {
                "unit": f"skfleet-builder-{name}.service",
                "service_argv": argv,
                "production": {"resources": RESOURCES},
            }
        )
    )
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def test_only_fresh_local_launches_are_charged(tmp_path, monkeypatch):
    monkeypatch.setattr(resources.socket, "gethostname", lambda: "chiap02")
    monkeypatch.setattr(
        resources.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="", returncode=0)
    )
    (tmp_path / "fleet/resource-admission/chiap02/local").mkdir(parents=True)
    (tmp_path / "fleet/resource-admission/chiap08/remote").mkdir(parents=True)
    _launch(tmp_path, "aaaaaaaa", "local")
    _launch(tmp_path, "bbbbbbbb", "remote")  # chiap08's launch, synced here
    _launch(tmp_path, "cccccccc", "local", age=3600 + 2000)  # past RuntimeMaxSec
    units = resources.active_resource_units(tmp_path)
    assert [u["unit"] for u in units] == ["skfleet-builder-aaaaaaaa.service"]


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(resources.socket, "gethostname", lambda: "chiap02")
    monkeypatch.setattr(
        resources.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="", returncode=0)
    )
    (tmp_path / "fleet/resource-admission/chiap02/local").mkdir(parents=True)


def _open_up(tmp_path, name):
    (tmp_path / "fleet/test-runs" / name).chmod(0o755)  # Syncthing-style mode


def test_non_private_expired_dir_is_skipped(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "aaaaaaaa", "local")
    _launch(tmp_path, "cccccccc", "local", age=3600 + 2000)
    _open_up(tmp_path, "cccccccc")
    units = resources.active_resource_units(tmp_path)
    assert [u["unit"] for u in units] == ["skfleet-builder-aaaaaaaa.service"]


def test_non_private_terminal_dir_is_skipped(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "aaaaaaaa", "local")
    _launch(tmp_path, "dddddddd", "local")
    (tmp_path / "fleet/test-runs/dddddddd/terminal.json").write_text("{}")
    _open_up(tmp_path, "dddddddd")
    units = resources.active_resource_units(tmp_path)
    assert [u["unit"] for u in units] == ["skfleet-builder-aaaaaaaa.service"]


def test_non_private_live_dir_is_charged_without_aborting(tmp_path, monkeypatch, caplog):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "aaaaaaaa", "local")
    _launch(tmp_path, "eeeeeeee", "local")
    _open_up(tmp_path, "eeeeeeee")
    with caplog.at_level("WARNING"):
        units = {u["unit"]: u for u in resources.active_resource_units(tmp_path)}
    assert set(units) == {"skfleet-builder-aaaaaaaa.service", "skfleet-builder-eeeeeeee.service"}
    assert (
        units["skfleet-builder-eeeeeeee.service"]["reserved_memory_max"]
        == RESOURCES["memory_max_bytes"]
    )
    assert "eeeeeeee" in caplog.text


def test_non_private_live_dir_with_garbage_is_charged_default(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "ffffffff", "local")
    (tmp_path / "fleet/test-runs/ffffffff/launch.json").write_text("not json")
    _open_up(tmp_path, "ffffffff")
    (unit,) = resources.active_resource_units(tmp_path)
    assert unit["unit"] == "skfleet-builder-ffffffff.service"
    assert unit["reserved_memory_max"] == resources.UNTRUSTED_LAUNCH_MEMORY_BYTES


def test_non_private_live_dir_charges_conservatively_in_occupancy(tmp_path, monkeypatch):
    from skcapstone.fleet import production_admission as admission

    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "eeeeeeee", "local")
    _open_up(tmp_path, "eeeeeeee")
    root = tmp_path / "fleet/resource-admission/chiap02"
    (root / "local").rmdir()  # no reservations: only the launch inventory is charged
    launch = tmp_path / "fleet/test-runs/eeeeeeee/launch.json"
    data = json.loads(launch.read_text())
    data["service_argv"] = []  # legacy unfenced launch: charged on every host
    launch.write_text(json.dumps(data))
    for d in (root, root.parent, root.parent.parent):
        d.chmod(0o700)
    rows = admission._occupancy(root, tmp_path, strict_terminal=True)
    assert [r["unit"] for r in rows] == ["skfleet-builder-eeeeeeee.service"]
