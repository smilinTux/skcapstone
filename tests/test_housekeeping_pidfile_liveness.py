"""Regression tests — prune_derived_junk MUST NOT delete a LIVE pidfile.

F1 (HIGH): the ``*.pid`` sweep previously unlinked every pidfile under the
profile tree with no liveness check, including the running daemon's own
``daemon.pid`` (housekeeping runs hourly with shared_root=~/.skcapstone). A
pidfile whose PID is still alive must be preserved; only dead/garbage/empty
pidfiles are junk.
"""

from __future__ import annotations

import os

from skcapstone.housekeeping import (
    _count_derived_junk,
    prune_derived_junk,
)


def _dead_pid() -> int:
    """Return a PID that is (almost certainly) not alive."""
    pid = 2_000_000
    while pid > 1:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except OSError:
            # exists (or EPERM) — try a lower one
            pid -= 1
            continue
        pid -= 1
    return 999_999


class TestLivePidfilePreserved:
    def test_live_pidfile_not_deleted(self, tmp_path):
        pid = tmp_path / "daemon.pid"
        pid.write_text(str(os.getpid()), encoding="utf-8")  # our own live PID

        removed = prune_derived_junk(tmp_path)

        assert pid.exists(), "a live daemon's pidfile must never be deleted"
        assert removed == 0

    def test_live_pid_not_counted_in_dry_run(self, tmp_path):
        pid = tmp_path / "daemon.pid"
        pid.write_text(str(os.getpid()), encoding="utf-8")
        assert _count_derived_junk(tmp_path) == 0


class TestDeadPidfileRemoved:
    def test_dead_pid_deleted(self, tmp_path):
        pid = tmp_path / "stale.pid"
        pid.write_text(str(_dead_pid()), encoding="utf-8")

        removed = prune_derived_junk(tmp_path)

        assert not pid.exists(), "a dead pidfile is junk and must be removed"
        assert removed >= 1

    def test_garbage_pid_deleted(self, tmp_path):
        pid = tmp_path / "garbage.pid"
        pid.write_text("not-a-pid", encoding="utf-8")

        removed = prune_derived_junk(tmp_path)

        assert not pid.exists()
        assert removed >= 1

    def test_empty_pid_deleted(self, tmp_path):
        pid = tmp_path / "empty.pid"
        pid.write_text("", encoding="utf-8")

        removed = prune_derived_junk(tmp_path)

        assert not pid.exists()
        assert removed >= 1

    def test_mixed_live_and_dead(self, tmp_path):
        live = tmp_path / "live.pid"
        live.write_text(str(os.getpid()), encoding="utf-8")
        dead = tmp_path / "dead.pid"
        dead.write_text(str(_dead_pid()), encoding="utf-8")

        removed = prune_derived_junk(tmp_path)

        assert live.exists()
        assert not dead.exists()
        assert removed == 1
