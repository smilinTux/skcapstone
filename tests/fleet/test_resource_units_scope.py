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
