"""Tests for the cross-host seat lease (spec 2026-09-18-seat-lease, Task 1).

Two layers, tested separately:

1. ``_locked_apply`` is the pure read-modify-write decision, the thing that
   actually runs on the lease host once ``ssh`` lands there. It is tested
   directly, with real temp files and (for the concurrency test) real
   threads and a real ``fcntl.flock`` -- no ``Runner`` involved, because
   there is no subprocess between the test and the code that matters here.
   This is where "two simultaneous acquirers cannot both win" gets proven:
   for real, against the real lock, not against a mocked stand-in for one.

2. ``acquire``/``renew``/``release``/``current_holder`` are the public API
   that a caller on a DIFFERENT host would use. Those are tested through a
   ``FakeRunner`` that records the ``ssh``-wrapped command and replies with
   canned JSON, exactly the way ``test_staged_rollout.py`` fakes ``ssh``
   calls for ``default_deploy_node``. No test here ever shells out, to a
   real host or otherwise.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from skcapstone.fleet import seat_lease
from skcapstone.fleet.seat_lease import LeaseResult, _locked_apply

# --------------------------------------------------------------------------
# layer 1: _locked_apply, the real critical section
# --------------------------------------------------------------------------


@pytest.fixture
def lease_paths(tmp_path: Path) -> tuple[str, str]:
    return (str(tmp_path / "seat-lease.json"), str(tmp_path / "seat-lease.lock"))


def test_acquire_on_an_empty_lease_succeeds(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    result = _locked_apply("acquire", "chiap08", 300.0, lease_path, lock_path)
    assert result["ok"] is True
    assert result["holder"] == "chiap08"
    assert result["remaining_seconds"] == pytest.approx(300.0)


def test_a_live_lease_blocks_a_different_acquirer(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 300.0, lease_path, lock_path)

    blocked = _locked_apply("acquire", "chiap02", 300.0, lease_path, lock_path)

    assert blocked["ok"] is False
    assert blocked["holder"] == "chiap08"
    assert blocked["remaining_seconds"] == pytest.approx(300.0, abs=1.0)


def test_acquire_by_the_current_live_holder_is_not_an_error(lease_paths: tuple[str, str]) -> None:
    """Re-acquiring your own live lease is a normal renewal-shaped success,
    not the "someone else holds it" refusal.
    """
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 300.0, lease_path, lock_path)

    again = _locked_apply("acquire", "chiap08", 600.0, lease_path, lock_path)

    assert again["ok"] is True
    assert again["holder"] == "chiap08"
    assert again["remaining_seconds"] == pytest.approx(600.0)


def test_an_expired_lease_is_acquirable_by_anyone(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 1.0, lease_path, lock_path)
    time.sleep(1.2)

    result = _locked_apply("acquire", "chiap02", 300.0, lease_path, lock_path)

    assert result["ok"] is True
    assert result["holder"] == "chiap02"


def test_renew_by_the_holder_succeeds_and_extends(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 5.0, lease_path, lock_path)

    renewed = _locked_apply("renew", "chiap08", 300.0, lease_path, lock_path)

    assert renewed["ok"] is True
    assert renewed["holder"] == "chiap08"
    assert renewed["remaining_seconds"] == pytest.approx(300.0)


def test_renew_by_a_non_holder_fails(lease_paths: tuple[str, str]) -> None:
    """The rule this whole primitive exists to enforce: a host that lost
    the lease cannot steal it back by calling renew instead of acquire.
    """
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 300.0, lease_path, lock_path)

    stolen = _locked_apply("renew", "chiap02", 300.0, lease_path, lock_path)

    assert stolen["ok"] is False
    assert stolen["holder"] == "chiap08"


def test_renew_of_an_expired_lease_fails_even_for_the_former_holder(
    lease_paths: tuple[str, str],
) -> None:
    """Once expired there is no live holder to renew as: the former holder
    must go back through acquire, the same as any other caller.
    """
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 1.0, lease_path, lock_path)
    time.sleep(1.2)

    result = _locked_apply("renew", "chiap08", 300.0, lease_path, lock_path)

    assert result["ok"] is False
    assert result["holder"] is None


def test_renew_with_no_lease_at_all_fails(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    result = _locked_apply("renew", "chiap08", 300.0, lease_path, lock_path)
    assert result["ok"] is False
    assert result["holder"] is None


def test_release_by_the_holder_clears_it(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 300.0, lease_path, lock_path)

    _locked_apply("release", "chiap08", 0.0, lease_path, lock_path)

    after = _locked_apply("current_holder", "", 0.0, lease_path, lock_path)
    assert after["holder"] is None


def test_release_by_a_non_holder_does_not_clear_it(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 300.0, lease_path, lock_path)

    _locked_apply("release", "chiap02", 0.0, lease_path, lock_path)

    after = _locked_apply("current_holder", "", 0.0, lease_path, lock_path)
    assert after["holder"] == "chiap08"


def test_current_holder_of_an_untouched_lease_is_none(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    result = _locked_apply("current_holder", "", 0.0, lease_path, lock_path)
    assert result["holder"] is None
    assert result["remaining_seconds"] is None


def test_current_holder_of_an_expired_lease_is_none(lease_paths: tuple[str, str]) -> None:
    lease_path, lock_path = lease_paths
    _locked_apply("acquire", "chiap08", 1.0, lease_path, lock_path)
    time.sleep(1.2)

    result = _locked_apply("current_holder", "", 0.0, lease_path, lock_path)

    assert result["holder"] is None


def test_two_simultaneous_acquirers_cannot_both_succeed(lease_paths: tuple[str, str]) -> None:
    """The real concurrency proof: two threads race to acquire the same
    lease through the same flock-guarded critical section. Exactly one
    must win, every time, not "usually".
    """
    lease_path, lock_path = lease_paths
    outcomes: list[dict] = [None, None]  # type: ignore[list-item]
    start = threading.Barrier(2)

    def race(index: int, holder: str) -> None:
        start.wait()
        outcomes[index] = _locked_apply("acquire", holder, 300.0, lease_path, lock_path)

    threads = [
        threading.Thread(target=race, args=(0, "chiap08")),
        threading.Thread(target=race, args=(1, "chiap02")),
    ]
    for _ in range(25):
        # Reset between rounds so a single lucky interleaving doesn't hide
        # a real race: run this enough times that a broken critical
        # section would show two winners at least once.
        for p in (lease_path, lock_path):
            Path(p).unlink(missing_ok=True)
        outcomes = [None, None]
        threads = [
            threading.Thread(target=race, args=(0, "chiap08")),
            threading.Thread(target=race, args=(1, "chiap02")),
        ]
        start.reset()
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [o["holder"] for o in outcomes if o["ok"]]
        assert len(winners) == 1, f"expected exactly one winner, got {outcomes}"


# --------------------------------------------------------------------------
# layer 2: the public ssh-wrapped API
# --------------------------------------------------------------------------


class FakeRunner:
    """Records every command; replies from a canned queue of JSON payloads."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)
        payload = self.replies.pop(0)
        return CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")


def test_acquire_ships_one_ssh_call_to_the_lease_host() -> None:
    runner = FakeRunner(
        [{"ok": True, "holder": "chiap08", "remaining_seconds": 300.0, "reason": "acquired"}]
    )

    result = seat_lease.acquire("chiap01", "chiap08", 300.0, runner=runner)

    assert result == LeaseResult(
        ok=True, holder="chiap08", remaining_seconds=300.0, reason="acquired"
    )
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[0] == "ssh"
    assert cmd[1] == "chiap01"
    joined = " ".join(cmd)
    assert "flock" in joined or "_locked_apply" in joined
    assert "chiap08" in joined


def test_acquire_blocked_reports_the_other_holder_not_an_exception() -> None:
    runner = FakeRunner(
        [{"ok": False, "holder": "chiap08", "remaining_seconds": 240.0, "reason": "held"}]
    )

    result = seat_lease.acquire("chiap01", "chiap02", 300.0, runner=runner)

    assert result.ok is False
    assert result.holder == "chiap08"
    assert result.remaining_seconds == 240.0


def test_renew_ships_the_holder_and_ttl() -> None:
    runner = FakeRunner(
        [{"ok": True, "holder": "chiap08", "remaining_seconds": 300.0, "reason": "renewed"}]
    )

    result = seat_lease.renew("chiap01", "chiap08", 300.0, runner=runner)

    assert result.ok is True
    joined = " ".join(runner.calls[0])
    assert "renew" in joined


def test_release_returns_none_and_still_ships_a_call() -> None:
    runner = FakeRunner(
        [{"ok": True, "holder": None, "remaining_seconds": None, "reason": "released"}]
    )

    result = seat_lease.release("chiap01", "chiap08", runner=runner)

    assert result is None
    assert len(runner.calls) == 1


def test_current_holder_returns_the_bare_name() -> None:
    runner = FakeRunner(
        [{"ok": True, "holder": "chiap08", "remaining_seconds": 100.0, "reason": "held"}]
    )

    holder = seat_lease.current_holder("chiap01", runner=runner)

    assert holder == "chiap08"


def test_current_holder_of_an_empty_lease_is_none() -> None:
    runner = FakeRunner(
        [{"ok": True, "holder": None, "remaining_seconds": None, "reason": "empty"}]
    )

    holder = seat_lease.current_holder("chiap01", runner=runner)

    assert holder is None


def test_a_nonzero_exit_from_ssh_degrades_to_a_failed_result_not_a_raise() -> None:
    def runner(cmd: list[str]) -> CompletedProcess:
        return CompletedProcess(
            cmd, 255, stdout="", stderr="ssh: connect to host chiap01: timed out"
        )

    result = seat_lease.acquire("chiap01", "chiap08", 300.0, runner=runner)

    assert result.ok is False
    assert result.holder is None
    assert "timed out" in result.reason


def test_a_runner_exception_degrades_to_a_failed_result_not_a_raise() -> None:
    def runner(cmd: list[str]) -> CompletedProcess:
        raise OSError("no route to host")

    result = seat_lease.acquire("chiap01", "chiap08", 300.0, runner=runner)

    assert result.ok is False
    assert "no route to host" in result.reason


def test_acquire_rejects_an_unsafe_host_name() -> None:
    with pytest.raises(ValueError):
        seat_lease.acquire("../etc", "chiap08", 300.0, runner=FakeRunner([]))


def test_acquire_rejects_an_unsafe_holder_name() -> None:
    with pytest.raises(ValueError):
        seat_lease.acquire("chiap01", "../etc", 300.0, runner=FakeRunner([]))
