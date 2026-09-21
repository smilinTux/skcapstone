"""Regression tests for fair ownership of the shared fleet rotation lock."""

from __future__ import annotations

import fcntl
import inspect
import threading
import time
from pathlib import Path

from skcapstone.fleet.rotation_lock import SERAPH_LOCK_WAIT_SECONDS, acquire_rotation_lock
from skcapstone.niobe_live_entrypoint import _DISPATCH_TIMEOUT_SECONDS
from skcapstone.seat_cycle_entrypoint import _SERAPH_DISPATCH_TIMEOUT_SECONDS


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


def test_waiting_seraph_acquires_after_tank_without_concurrent_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Seraph waits for a Tank holder while the shared lock stays exclusive."""

    path = tmp_path / "rotate.lock"
    active = 0
    peak = 0
    order: list[str] = []
    state_lock = threading.Lock()
    tank_started = threading.Event()
    seraph_blocked = threading.Event()
    release_tank = threading.Event()
    real_flock = fcntl.flock

    def observed_flock(fd: int, operation: int) -> None:
        try:
            real_flock(fd, operation)
        except BlockingIOError:
            if threading.current_thread().name == "waiting-seraph":
                seraph_blocked.set()
            raise

    monkeypatch.setattr("skcapstone.fleet.rotation_lock.fcntl.flock", observed_flock)

    def mutate(seat: str, wait_seconds: float) -> None:
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
                if seat == "tank":
                    tank_started.set()
            if seat == "tank":
                assert release_tank.wait(timeout=1)
            with state_lock:
                active -= 1
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    tank = threading.Thread(target=mutate, args=("tank", 0))
    tank.start()
    assert tank_started.wait(timeout=1)

    seraph = threading.Thread(target=mutate, args=("seraph", 0.5), name="waiting-seraph")
    seraph.start()
    assert seraph_blocked.wait(timeout=1)
    assert seraph.is_alive()
    release_tank.set()

    tank.join(timeout=1)
    seraph.join(timeout=1)

    assert not tank.is_alive() and not seraph.is_alive()
    assert order == ["tank", "seraph"]
    assert peak == 1


def test_tank_and_atlas_remain_nonblocking(tmp_path: Path) -> None:
    """Only Niobe and Seraph receive bounded shared-lock waiting."""

    path = tmp_path / "rotate.lock"
    holder = acquire_rotation_lock(path, seat="niobe", wait_seconds=0)
    assert holder is not None
    try:
        for seat in ("tank", "atlas"):
            started = time.monotonic()
            assert acquire_rotation_lock(path, seat=seat, wait_seconds=0.1) is None
            assert time.monotonic() - started < 0.05
    finally:
        holder.close()


def test_dispatcher_routes_niobe_and_seraph_through_safe_bounded_waits() -> None:
    """Production wiring keeps one shared lock and derives its governed seat."""

    root = Path(__file__).parents[1]
    source = (root / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    services = [
        (root / "systemd/skfleet-niobe-live.service").read_text(),
        (root / "src/skcapstone/data/systemd/skfleet-niobe-live.service").read_text(),
    ]
    timers = [
        (root / "systemd/skfleet-niobe-live.timer").read_text(),
        (root / "src/skcapstone/data/systemd/skfleet-niobe-live.timer").read_text(),
    ]
    seraph = (root / "src/skcapstone/data/systemd/skfleet-seraph.service").read_text()
    assert 'Path(HOME)/".skcapstone/fleet/rotate.lock"' in source
    assert 'seat=ONLY_SEAT or "niobe"' in source
    lock_wait = inspect.signature(acquire_rotation_lock).parameters["wait_seconds"].default
    dispatcher_deadline = _DISPATCH_TIMEOUT_SECONDS
    service_deadline = 300
    assert lock_wait < dispatcher_deadline < service_deadline
    assert all(f"TimeoutStartSec={service_deadline}" in service for service in services)
    assert all("Persistent=false" in timer for timer in timers)
    assert all("Unit=skfleet-niobe-live.service" in timer for timer in timers)
    cleanup_margin = 30
    # Seraph carries its OWN deadline: its budget was raised to 540s (with the
    # timer cadence to 600s) on 2026-09-20 because measured cycles ran 150-188s
    # against the old 190s dispatcher timeout and seraph_dispatch_timeout was
    # the dominant cycle outcome. niobe-live is unchanged at 300s, so these two
    # literals are deliberately separate rather than one shared constant.
    seraph_service_deadline = 540
    assert f"TimeoutStartSec={seraph_service_deadline}" in seraph
    assert (
        SERAPH_LOCK_WAIT_SECONDS + _SERAPH_DISPATCH_TIMEOUT_SECONDS + cleanup_margin
        < seraph_service_deadline
    )
    assert "SKFLEET_SERAPH_BATCH_SIZE=2" in seraph
