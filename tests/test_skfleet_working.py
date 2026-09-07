"""Seat-agent filter and assess behavior for skfleet-working."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

PATH = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-working.py"


def load_monitor():
    loader = importlib.machinery.SourceFileLoader("skfleet_working", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_seat_agent_is_not_ephemeral_worker_name() -> None:
    monitor = load_monitor()
    assert monitor.is_ephemeral_worker_agent("pi-chiap08", "a1b2c3d4") is False
    assert monitor.is_ephemeral_worker_agent("pi-jarvis", "deadbeef") is False


def test_ephemeral_worker_name_matches_card_suffix() -> None:
    monitor = load_monitor()
    assert monitor.is_ephemeral_worker_agent("pi-codex-chiap08-a1b2c3d4", "a1b2c3d4") is True
    assert monitor.is_ephemeral_worker_agent("a1b2c3d4", "a1b2c3d4") is True


def test_remote_collector_keeps_seat_filter_in_sync() -> None:
    monitor = load_monitor()
    assert "Only ephemeral workers are named" in monitor.REMOTE
    assert "agent.endswith('-'+card) or agent == card" in monitor.REMOTE
    assert "is_ephemeral_worker_agent" in PATH.read_text(encoding="utf-8")


def test_released_and_superseded_units_are_actionable_without_samples() -> None:
    monitor = load_monitor()
    for claim_state in ("released", "superseded"):
        worker = monitor.Worker(
            host="chiap08", agent="unit", card="a1b2c3d4", pid=0,
            elapsed=0, cpu=0, log_bytes=-1, log_age=-1,
            unit="skfleet-worker-a1b2c3d4.service", tmux=False,
            claim_state=claim_state, card_status="released",
            unit_missing_process=True,
        )
        state, _ = monitor.assess(worker, {}, now=100)
        assert state == "ACTION REQUIRED"


def test_current_finalizer_keeps_bounded_settling_grace() -> None:
    monitor = load_monitor()
    worker = monitor.Worker(
        host="chiap08", agent="unit", card="a1b2c3d4", pid=0,
        elapsed=0, cpu=0, log_bytes=-1, log_age=-1,
        unit="skfleet-worker-a1b2c3d4.service", tmux=False,
        claim_state="exact", card_status="doing", unit_missing_process=True,
        unit_timestamp=95,
    )
    samples = {}
    state, first = monitor.assess(worker, samples, now=100)
    assert state == "SETTLING" and first == 95
    state, _ = monitor.assess(worker, samples, now=110)
    assert state == "SETTLING"
    state, _ = monitor.assess(worker, samples, now=126)
    assert state == "ACTION REQUIRED"


def test_repeated_invocation_preserves_first_observation() -> None:
    monitor = load_monitor()
    worker = monitor.Worker(
        host="chiap08", agent="unit", card="a1b2c3d4", pid=0,
        elapsed=0, cpu=0, log_bytes=-1, log_age=-1,
        unit="skfleet-worker-a1b2c3d4.service", tmux=False,
        claim_state="exact", card_status="doing", unit_missing_process=True,
    )
    samples = {}
    assert monitor.assess(worker, samples, now=100)[0] == "SETTLING"
    assert monitor.assess(worker, samples, now=105)[0] == "SETTLING"
    assert samples["chiap08/skfleet-worker-a1b2c3d4.service"]["first_seen"] == 100


def test_seat_hold_without_unit_is_not_stale_projection_candidate() -> None:
    """Seat agents with hold/current_task must not enter the stale path."""
    monitor = load_monitor()
    # Without the filter, a seat projection would reach assess as unit=not-found.
    # The filter excludes them before emission; only ephemeral names qualify.
    assert monitor.is_ephemeral_worker_agent("pi-chiap08", "a1b2c3d4") is False
    ephemeral = monitor.Worker(
        host="chiap08",
        agent="pi-codex-chiap08-a1b2c3d4",
        card="a1b2c3d4",
        pid=0,
        elapsed=0,
        cpu=0,
        log_bytes=-1,
        log_age=-1,
        unit="not-found",
        tmux=False,
        claim_state="mismatch",
        card_status="doing",
        unit_missing_process=False,
        evidence_source="agent-projection+systemd+proc",
    )
    state, _ = monitor.assess(ephemeral, {}, now=100)
    assert state == "STALE PROJECTION"
