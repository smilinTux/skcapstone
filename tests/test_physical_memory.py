"""Physical Windows host bounds are mandatory for WSL admissions."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from skcapstone.fleet import capacity, physical_memory, workspace_runtime

MEM = (
    "MemTotal: 16000000 kB\nMemAvailable: 12000000 kB\n"
    "SwapTotal: 2000000 kB\nSwapFree: 1000000 kB\n"
)


def reply(data, code=0):
    """Return a bounded synthetic CIM response."""
    return lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=json.dumps(data))


def test_low_physical_memory_denies_high_guest_capacity():
    """A roomy WSL guest cannot consume its exhausted Windows host."""
    text = physical_memory.constrain_meminfo(
        MEM,
        kernel_release="6.6-microsoft-standard-WSL2",
        runner=reply({"total_kb": 8000000, "available_kb": 500000}),
    )
    assert capacity.admit_headroom(text)[0] is False
    assert "MemAvailable: 0 kB" in text
    assert "SwapFree: 1000000 kB" in text


def test_physical_and_guest_minimum_preserves_both_bounds():
    """Physical reserve is charged before applying guest limits."""
    text = physical_memory.constrain_meminfo(
        MEM,
        kernel_release="Microsoft",
        runner=reply({"total_kb": 16000000, "available_kb": 5000000}),
    )
    assert "MemAvailable: 3400000 kB" in text
    text = physical_memory.constrain_meminfo(
        MEM,
        kernel_release="Microsoft",
        runner=reply({"total_kb": 64000000, "available_kb": 60000000}),
    )
    assert "MemAvailable: 12000000 kB" in text


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"total_kb": 10, "available_kb": 11},
        {"total_kb": True, "available_kb": 1},
        {"total_kb": 100, "available_kb": -1},
        {"total_kb": "100", "available_kb": 50},
    ],
)
def test_malformed_physical_evidence_fails_closed(data):
    """Invalid host evidence cannot fall back to guest memory."""
    with pytest.raises(ValueError):
        physical_memory.constrain_meminfo(MEM, kernel_release="Microsoft", runner=reply(data))


def test_timeout_propagates_and_native_linux_never_probes():
    """Timeouts deny WSL; native Linux retains its exact input."""

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)

    with pytest.raises(subprocess.TimeoutExpired):
        physical_memory.constrain_meminfo(MEM, kernel_release="Microsoft", runner=timeout)
    assert (
        physical_memory.constrain_meminfo(MEM, kernel_release="6.8-generic", runner=timeout) == MEM
    )


def test_workspace_live_reader_uses_clamp(monkeypatch):
    """The actual admission reader invokes the shared host guard."""
    monkeypatch.setattr(physical_memory, "constrain_meminfo", lambda text: "clamped")
    assert workspace_runtime.read_meminfo() == "clamped"


def test_workspace_probe_failure_becomes_admission_failure(monkeypatch):
    """Interop failure is a native workspace error, never a fallback."""

    def fail(text):
        raise ValueError("host unavailable")

    monkeypatch.setattr(physical_memory, "constrain_meminfo", fail)
    with pytest.raises(workspace_runtime.WorkspaceRuntimeError):
        workspace_runtime.read_meminfo()


def test_advertisement_is_zero_without_required_evidence(monkeypatch):
    """A failed physical probe cannot advertise WSL spare capacity."""

    def fail():
        raise ValueError("host unavailable")

    monkeypatch.setattr(physical_memory, "physical_headroom_kb", fail)
    monkeypatch.setattr(capacity, "_gpu_info", lambda: None)
    assert capacity.node_capacity()["ram_gb"] == 0
