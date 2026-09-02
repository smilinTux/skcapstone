from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

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
    assert "current_task" in source
    assert "DUPLICATE card process" in source
    assert "ORPHAN: no unit or tmux owner" in source


def test_log_age_is_never_a_stall_predicate() -> None:
    source = PATH.read_text()
    assert "output buffered until completion" in source
    assert "log age is informational only" in source
    assert "NEVER STARTED" not in source
    assert "<-- stale" not in source
