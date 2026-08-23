"""Tests for the capacity probe and condition derivation."""

from __future__ import annotations

import sys
import types

from skcapstone.fleet import capacity, conditions

NOW = "2026-07-27T12:00:00Z"


def test_node_capacity_shape() -> None:
    cap = capacity.node_capacity()
    assert set(cap) == {"cores", "ram_gb", "disk_gb", "gpu", "vram_gb"}
    assert cap["cores"] >= 1 and cap["ram_gb"] > 0 and cap["disk_gb"] > 0


def test_node_capacity_reuses_autoscale_when_importable(monkeypatch) -> None:
    fake_autoscale = types.ModuleType("skharness.autocode.autoscale")
    fake_autoscale.resources = lambda: {"cores": 9, "ram_gb": 42.0, "disk_gb": 7.0}
    fake_autocode = types.ModuleType("skharness.autocode")
    fake_autocode.autoscale = fake_autoscale
    fake_skharness = types.ModuleType("skharness")
    fake_skharness.autocode = fake_autocode
    monkeypatch.setitem(sys.modules, "skharness", fake_skharness)
    monkeypatch.setitem(sys.modules, "skharness.autocode", fake_autocode)
    monkeypatch.setitem(sys.modules, "skharness.autocode.autoscale", fake_autoscale)
    monkeypatch.setattr(capacity, "_gpu_info", lambda: None)
    cap = capacity.node_capacity()
    assert cap["cores"] == 9 and cap["ram_gb"] == 42.0 and cap["disk_gb"] == 7.0


def test_fallback_matches_autoscale_shape() -> None:
    fb = capacity._fallback_resources()
    assert set(fb) == {"cores", "ram_gb", "disk_gb"}


def test_fallback_survives_unreadable_meminfo_and_disk_usage(monkeypatch) -> None:
    import shutil
    from pathlib import Path

    def boom_read_text(self, *a, **k):
        raise OSError("no /proc/meminfo here")

    def boom_disk_usage(*a, **k):
        raise OSError("no disk stats here")

    monkeypatch.setattr(Path, "read_text", boom_read_text)
    monkeypatch.setattr(shutil, "disk_usage", boom_disk_usage)
    fb = capacity._fallback_resources()
    assert fb["ram_gb"] == 8.0
    assert fb["disk_gb"] == 20.0


def test_gpu_probe_absent_is_none(monkeypatch) -> None:
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", boom)
    assert capacity._gpu_info() is None


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_gpu_probe_present_parses_name_and_vram(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(0, "RTX 5060 Ti, 16384\n"),
    )
    assert capacity._gpu_info() == {"name": "RTX 5060 Ti", "vram_gb": 16.0}


def test_gpu_probe_malformed_output_returns_none(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(0, "RTX 5060 Ti, not-a-number\n"),
    )
    assert capacity._gpu_info() is None


def test_gpu_probe_nonzero_returncode_returns_none(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _FakeCompletedProcess(1, ""),
    )
    assert capacity._gpu_info() is None


def _by_type(conds):
    return {c["type"]: c for c in conds}


def test_conditions_pressure_and_conflict(tmp_path) -> None:
    cap = {"cores": 4, "ram_gb": 1.0, "disk_gb": 100.0, "gpu": None, "vram_gb": None}
    conds = _by_type(conditions.node_conditions(cap, tmp_path, NOW))
    assert conds["Ready"]["status"] == "True"
    assert conds["MemoryPressure"]["status"] == "True"  # 1.0 < 2.0
    assert conds["DiskPressure"]["status"] == "False"
    assert conds["SyncConflict"]["status"] == "False"
    assert "GPUAvailable" not in conds
    (tmp_path / "x.sync-conflict-20260727").write_text("boom")
    conds = _by_type(conditions.node_conditions(cap, tmp_path, NOW))
    assert conds["SyncConflict"]["status"] == "True"


def test_gpu_condition_when_present(tmp_path) -> None:
    cap = {"cores": 4, "ram_gb": 8.0, "disk_gb": 100.0, "gpu": "RTX 5060 Ti", "vram_gb": 16.0}
    conds = _by_type(conditions.node_conditions(cap, tmp_path, NOW))
    assert conds["GPUAvailable"]["status"] == "True"


def test_merge_transitions_preserves_unchanged(tmp_path) -> None:
    old = [
        {
            "type": "Ready",
            "status": "True",
            "reason": "r",
            "message": "m",
            "lastTransition": "2026-07-26T00:00:00Z",
        }
    ]
    new = [
        {"type": "Ready", "status": "True", "reason": "r", "message": "m", "lastTransition": NOW}
    ]
    merged = conditions.merge_transitions(new, old)
    assert merged[0]["lastTransition"] == "2026-07-26T00:00:00Z"
    flipped = [
        {"type": "Ready", "status": "False", "reason": "r", "message": "m", "lastTransition": NOW}
    ]
    assert conditions.merge_transitions(flipped, old)[0]["lastTransition"] == NOW
