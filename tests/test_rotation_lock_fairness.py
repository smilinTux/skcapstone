"""Regression tests for fair ownership of the shared fleet rotation lock."""

from __future__ import annotations

import fcntl
import threading
import time
from pathlib import Path

from skcapstone.fleet.rotation_lock import acquire_rotation_lock


def test_waiting_niobe_gets_next_dispatch_without_concurrent_mutation(tmp_path: Path) -> None:
    """A long Seraph cycle cannot turn each Niobe timer beat into a no-op."""

    path = tmp_path / "rotate.lock"
    active = 0
    peak = 0
    order: list[str] = []
    state_lock = threading.Lock()
    seraph_started = threading.Event()
    release_seraph = threading.Event()

    def mutate(seat: str, hold_seconds: float, wait_seconds: float) -> None:
        nonlocal active, peak
        lock = acquire_rotation_lock(path, seat=seat, wait_seconds=wait_seconds)
        if lock is None:
            order.append(f"{seat}:overlap")
            return
        try:
            with state_lock:
                active += 1
                peak = max(peak, active)
                order.append(seat)
                if seat == "seraph":
                    seraph_started.set()
            if seat == "seraph" and hold_seconds:
                assert release_seraph.wait(timeout=1)
            else:
                time.sleep(hold_seconds)
            with state_lock:
                active -= 1
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    long_seraph = threading.Thread(target=mutate, args=("seraph", 0.12, 0))
    long_seraph.start()
    assert seraph_started.wait(timeout=1)

    niobe = threading.Thread(target=mutate, args=("niobe", 0.01, 0.5))
    niobe.start()
    repeated_seraph = threading.Thread(target=mutate, args=("seraph", 0, 0))
    repeated_seraph.start()
    repeated_seraph.join(timeout=1)
    release_seraph.set()

    long_seraph.join(timeout=1)
    niobe.join(timeout=1)

    assert not long_seraph.is_alive() and not niobe.is_alive()
    assert order == ["seraph", "seraph:overlap", "niobe"]
    assert peak == 1


def test_niobe_wait_is_bounded_and_releases_lock(tmp_path: Path) -> None:
    """Niobe fails closed at its bound and a later cycle can still acquire."""

    path = tmp_path / "rotate.lock"
    holder = acquire_rotation_lock(path, seat="seraph", wait_seconds=0)
    assert holder is not None
    started = time.monotonic()
    assert acquire_rotation_lock(path, seat="niobe", wait_seconds=0.03) is None
    assert time.monotonic() - started < 0.2

    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    later = acquire_rotation_lock(path, seat="seraph", wait_seconds=0)
    assert later is not None
    later.close()


def test_dispatcher_routes_only_niobe_through_bounded_wait() -> None:
    """Production wiring keeps one shared lock and derives its governed seat."""

    root = Path(__file__).parents[1]
    source = (root / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    service = (root / "src/skcapstone/data/systemd/skfleet-niobe-live.service").read_text()
    seraph = (root / "src/skcapstone/data/systemd/skfleet-seraph.service").read_text()
    assert 'Path(HOME)/".skcapstone/fleet/rotate.lock"' in source
    assert 'seat=ONLY_SEAT or "niobe"' in source
    assert "TimeoutStartSec=300" in service
    assert "timeout=240" in (root / "src/skcapstone/seat_cycle_entrypoint.py").read_text(
        encoding="utf-8"
    )
    assert "SKFLEET_SERAPH_BATCH_SIZE=2" in seraph
