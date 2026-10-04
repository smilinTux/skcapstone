"""The WSL physical limit survives terminal-success accounting updates."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import physical_memory as physical
from skcapstone.fleet import production_resources as resources


def response(available=2_000_000, total=10_000_000):
    return SimpleNamespace(
        returncode=0, stdout=json.dumps(dict(total_kb=total, available_kb=available))
    )


def test_native_linux_never_calls_windows():
    def denied(*args, **kwargs):
        raise AssertionError("Windows probe on native Linux")

    text = "MemTotal: 10000000 kB\nMemAvailable: 8000000 kB\n"
    assert physical.constrain_meminfo(text, runner=denied, kernel_release="Linux") == text


@pytest.mark.parametrize("available,expected", [(2_000_000, 1_000_000), (1, 0)])
def test_windows_headroom_clamps_guest(available, expected):
    text = "MemTotal: 10000000 kB\nMemAvailable: 8000000 kB\n"
    actual = physical.constrain_meminfo(
        text, runner=lambda *a, **kw: response(available), kernel_release="microsoft"
    )
    assert actual == f"MemTotal: 10000000 kB\nMemAvailable: {expected} kB\n"


@pytest.mark.parametrize("value", [-1, 10_000_001, True, "2000000", None])
def test_invalid_windows_measurement_refuses(value):
    with pytest.raises(ValueError):
        physical.physical_headroom_kb(
            runner=lambda *a, **kw: response(value), kernel_release="microsoft"
        )


@pytest.mark.parametrize("failure", ["timeout", "invalid-json", "exit"])
def test_admission_denies_failed_physical_probe(monkeypatch, failure):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/sys/kernel/osrelease":
            return "microsoft"
        if str(path) == "/proc/meminfo":
            return "MemTotal: 10000000 kB\nMemAvailable: 8000000 kB\n"
        return original(path, *args, **kwargs)

    def runner(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("physical-probe", 5)
        return SimpleNamespace(returncode=int(failure == "exit"), stdout="invalid")

    monkeypatch.setattr(Path, "read_text", read)
    assert resources.local_worker_admission(
        {"node_quotas": {"host": {"memory_max_bytes": 1024}}},
        "host",
        [],
        runner=runner,
    ) == (False, "node-resource-evidence-unavailable")


def test_admission_uses_physical_limit_and_keeps_pending_reservations(monkeypatch):
    monkeypatch.setattr(
        resources,
        "constrain_meminfo",
        lambda text, **kw: "MemTotal: 10000000 kB\nMemAvailable: 2000000 kB\n",
    )
    policy = {"node_quotas": {"host": {"memory_max_bytes": 500_000 * 1024}}}
    assert resources.local_worker_admission(policy, "host", [])[0]

    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="Id=pending.service\nActiveState=inactive\n")

    allowed, reason = resources.local_worker_admission(
        policy,
        "host",
        [{"unit": "pending.service", "reserved_memory_max": 900_000 * 1024}],
        runner=runner,
    )
    assert not allowed
    assert "memory_available=102400000" in reason
