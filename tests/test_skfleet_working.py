from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

PATH = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-working"


def load_monitor():
    loader = importlib.machinery.SourceFileLoader("skfleet_working", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_duration_is_compact() -> None:
    monitor = load_monitor()
    assert monitor.duration(9) == "9s"
    assert monitor.duration(69) == "1m09s"
    assert monitor.duration(3660) == "1h01m"


def test_monitor_uses_process_cgroup_and_claim_truth() -> None:
    source = PATH.read_text()
    assert "/proc/[0-9]*/comm" in source
    assert "skfleet-worker-" in source
    assert "_claim_revision" in source
    assert "DUPLICATE card process" in source
    assert "ORPHAN: no unit or tmux owner" in source
    assert "list-units" in source
    assert "TERMINAL STILL RUNNING" in source
    assert "request_log" in source
    assert "{'pid':>7}" in source


def test_collect_encodes_remote_program_and_parses_worker() -> None:
    monitor = load_monitor()
    payload = {
        "host": "chiap03",
        "agent": "pi-codex-chiap03-a1b2c3d4",
        "card": "a1b2c3d4",
        "pid": 42,
        "elapsed": 9,
        "cpu": 1,
        "log_bytes": 0,
        "log_age": 0,
        "unit": "skfleet-worker-codex-a1b2c3d4.service",
        "tmux": False,
        "claim_state": "exact",
        "card_status": "doing",
        "unit_missing_process": False,
    }
    result = Mock(returncode=0, stdout=__import__("json").dumps(payload) + "\n")
    with patch.object(monitor.subprocess, "run", return_value=result) as run:
        rows = monitor.collect("chiap03")
    assert rows == [monitor.Worker(**payload)]
    command = run.call_args.args[0]
    assert command[-3:-1] == ["python3", "-c"]
    assert "b64decode" in command[-1]


def test_collect_fails_closed_on_ssh_error() -> None:
    monitor = load_monitor()
    result = Mock(returncode=255, stdout="", stderr="unreachable")
    with patch.object(monitor.subprocess, "run", return_value=result):
        with pytest.raises(OSError, match="ssh collector failed"):
            monitor.collect("chiap99")


def test_log_age_is_never_a_stall_predicate() -> None:
    source = PATH.read_text()
    assert "output buffered until completion" in source
    assert "log age is informational only" in source
    assert "NEVER STARTED" not in source
    assert "<-- stale" not in source
