"""Synced test-run launches are charged only on their own host, and not forever."""

import json
import os
import signal
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
    _launch(tmp_path, "cccccccc", "local", age=2 * 86400)
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


def _forge(tmp_path, name, **fields):
    path = tmp_path / "fleet/test-runs" / name / "launch.json"
    data = json.loads(path.read_text())
    data.update(fields)
    path.write_text(json.dumps(data))
    _open_up(tmp_path, name)
    return path


def test_forged_tiny_memory_is_charged_at_least_default(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "aaaaaaaa", "local")
    _forge(tmp_path, "aaaaaaaa", production={"resources": {"memory_max_bytes": 1}})
    (unit,) = resources.active_resource_units(tmp_path)
    assert unit["reserved_memory_max"] >= 3 * 1024**3


def test_forged_larger_memory_only_raises_the_charge(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "aaaaaaaa", "local")
    _forge(tmp_path, "aaaaaaaa", production={"resources": {"memory_max_bytes": 9 * 1024**3}})
    (unit,) = resources.active_resource_units(tmp_path)
    assert unit["reserved_memory_max"] == 9 * 1024**3


def test_forged_runtime_and_foreign_marker_do_not_uncharge(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "aaaaaaaa", "local", age=7200)
    _forge(
        tmp_path,
        "aaaaaaaa",
        production={"resources": {"memory_max_bytes": 1, "runtime_max_seconds": 1}},
        service_argv=["--setenv=SKFLEET_ADMISSION_ID=elsewhere"],
    )
    (unit,) = resources.active_resource_units(tmp_path)
    assert unit["reserved_memory_max"] >= 3 * 1024**3


def test_forged_unit_name_does_not_displace_another_unit(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    live = "skfleet-builder-live0001.service"
    monkeypatch.setattr(
        resources.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            stdout=live + " loaded active running x\n", returncode=0
        ),
    )
    _launch(tmp_path, "live0001", "local")
    big = 6 * 1024**3
    (tmp_path / "fleet/test-runs/live0001/launch.json").write_text(
        json.dumps(
            {
                "unit": live,
                "service_argv": [],
                "production": {"resources": {**RESOURCES, "memory_max_bytes": big}},
            }
        )
    )
    _launch(tmp_path, "bbbbbbbb", "local")
    _forge(tmp_path, "bbbbbbbb", unit=live, production={"resources": {"memory_max_bytes": 1}})
    units = {u["unit"]: u for u in resources.active_resource_units(tmp_path)}
    assert units[live]["reserved_memory_max"] == big
    assert units["skfleet-builder-bbbbbbbb.service"]["reserved_memory_max"] >= 3 * 1024**3


def test_fifo_launch_json_neither_hangs_nor_uncharges(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "ffffffff", "local")
    path = tmp_path / "fleet/test-runs/ffffffff/launch.json"
    path.unlink()
    os.mkfifo(path)
    _open_up(tmp_path, "ffffffff")
    # A blocking open would hang forever: fail the test after 10 s instead.
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("hung on FIFO")))
    signal.alarm(10)
    try:
        (unit,) = resources.active_resource_units(tmp_path)
    finally:
        signal.alarm(0)
    assert unit["reserved_memory_max"] == resources.UNTRUSTED_LAUNCH_MEMORY_BYTES


def test_symlinked_launch_json_is_not_followed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _launch(tmp_path, "ffffffff", "local")
    path = tmp_path / "fleet/test-runs/ffffffff/launch.json"
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"production": {"resources": {"memory_max_bytes": 9 * 1024**3}}}))
    path.unlink()
    path.symlink_to(target)
    _open_up(tmp_path, "ffffffff")
    (unit,) = resources.active_resource_units(tmp_path)
    assert unit["reserved_memory_max"] == resources.UNTRUSTED_LAUNCH_MEMORY_BYTES
