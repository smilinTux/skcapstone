"""One deadline object for a whole dispatcher cycle.

The niobe seat dispatcher runs inside a fixed wrapper timeout (270 s) that is
itself inside a shared seat generation. Before this module only the launch loop
honoured ``cycle_budget_seconds``; every earlier phase could spend the whole
budget (pool_fold grew from 130 s to 213 s on 2026-10-13 and nothing stopped it),
so the wrapper killed the cycle before it launched anything or wrote its receipt.

``CycleDeadline`` is created once at cycle start. Every phase asks it two things:

* ``stop(phase, done, remaining)``: has this phase reached its soft deadline?
  A phase owns the share of the budget named in ``PHASE_FRACTIONS`` (a cumulative
  fraction of elapsed time), so an early phase cannot starve the launch phase.
  The first stop is logged once as ``PHASE_DEADLINE|host|phase|done=..|remaining=..``.
* ``mark(phase)``: record the phase boundary, and emit ``PHASE_BUDGET_WARN`` when
  one phase alone used more than ``warn_share`` of the budget, so slow growth is
  visible long before it causes a timeout.

Iterative phases persist a resume cursor (``write_cursor``) naming the first item
not reached, and start from it next cycle (``rotate_from``), so a phase that is
cut short rotates through its work instead of re-trying the same head forever.

With time to spare nothing here changes a decision: ``stop`` is False and the
cursor is cleared.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

#: Wrapper timeout the dispatcher runs under (niobe_live_entrypoint).
DEFAULT_HARD_TIMEOUT_S = 270.0
#: Time kept free after the budget for the final receipt and log writes.
DEFAULT_FINAL_RESERVE_S = 20.0
DEFAULT_WARN_SHARE = 0.4

#: Cumulative fraction of the budget by which each phase must stop admitting
#: work. The launch phase keeps the whole budget (its existing behaviour); the
#: phases before it leave it room.
PHASE_FRACTIONS: dict[str, float] = {
    "startup_sweeps": 0.25,
    "review_sweeps": 0.35,
    "pool_fold": 0.65,
    "pool_unblocks": 0.7,
    "remote_review_offers": 1.0,
    "builder_offers": 1.0,
    "lane_selection": 0.92,
    "lane_launch": 1.0,
    "review_observation": 1.0,
}


def _env_float(name: str, default: float, environ: dict[str, str] | None = None) -> float:
    raw = (os.environ if environ is None else environ).get(name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class CycleDeadline:
    """Monotonic cycle clock shared by every dispatcher phase."""

    def __init__(
        self,
        budget_s: float,
        *,
        started: float | None = None,
        hard_timeout_s: float = DEFAULT_HARD_TIMEOUT_S,
        final_reserve_s: float = DEFAULT_FINAL_RESERVE_S,
        warn_share: float = DEFAULT_WARN_SHARE,
        clock: Callable[[], float] = time.monotonic,
        emit: Callable[[str], None] | None = None,
        host: str = "-",
        state_dir: str | None = None,
        persist: bool = True,
        fractions: dict[str, float] | None = None,
    ) -> None:
        self.clock = clock
        self.started = clock() if started is None else started
        self.hard_timeout_s = float(hard_timeout_s)
        self.final_reserve_s = float(final_reserve_s)
        # The budget can never reach into the final-write reserve.
        self.budget_s = min(float(budget_s), self.hard_timeout_s - self.final_reserve_s)
        self.warn_share = float(warn_share)
        self.host = host
        self.state_dir = state_dir
        self.persist = persist
        self.fractions = dict(PHASE_FRACTIONS if fractions is None else fractions)
        self._emit = emit
        self._stopped: set[str] = set()
        self._mark = self.started
        self.timings: list[tuple[str, float]] = []

    @classmethod
    def from_environment(cls, budget_s: float, **kwargs: Any) -> CycleDeadline:
        """Build from SKFLEET_* overrides (the entrypoint exports the timeout)."""
        kwargs.setdefault(
            "hard_timeout_s", _env_float("SKFLEET_DISPATCH_TIMEOUT_S", DEFAULT_HARD_TIMEOUT_S)
        )
        kwargs.setdefault(
            "final_reserve_s", _env_float("SKFLEET_FINAL_RESERVE_S", DEFAULT_FINAL_RESERVE_S)
        )
        kwargs.setdefault(
            "warn_share", _env_float("SKFLEET_PHASE_BUDGET_WARN_SHARE", DEFAULT_WARN_SHARE)
        )
        return cls(budget_s, **kwargs)

    # -- clock ---------------------------------------------------------------
    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining(self) -> float:
        """Seconds of budget left (negative once exhausted)."""
        return self.budget_s - self.elapsed()

    def hard_remaining(self) -> float:
        """Seconds until the wrapper timeout, final reserve included."""
        return self.hard_timeout_s - self.elapsed()

    def limit_s(self, phase: str) -> float:
        return self.budget_s * self.fractions.get(phase, 1.0)

    def expired(self, phase: str | None = None) -> bool:
        limit = self.budget_s if phase is None else self.limit_s(phase)
        return self.elapsed() >= limit

    # -- logging -------------------------------------------------------------
    def _log(self, line: str) -> None:
        try:
            if self._emit is not None:
                self._emit(line)
        except Exception:
            pass  # observability must never stop a cycle

    # -- stop / budget -------------------------------------------------------
    def stop(
        self, phase: str, done: int, remaining: int | None = None, *, min_done: int = 0
    ) -> bool:
        """True when ``phase`` is past its soft deadline and has done ``min_done`` items.

        ``min_done`` guarantees progress: a phase that always starts late still
        handles its first items, and the resume cursor moves it on next cycle.
        """
        if done < min_done or not self.expired(phase):
            return False
        self.report(phase, done, remaining)
        return True

    def report(self, phase: str, done: int, remaining: int | None = None) -> None:
        """Log PHASE_DEADLINE once per phase (also for caller-owned deadlines)."""
        if phase in self._stopped:
            return
        self._stopped.add(phase)
        self._log(
            "PHASE_DEADLINE|%s|%s|done=%d|remaining=%s|elapsed_s=%.1f|limit_s=%.1f"
            % (
                self.host,
                phase,
                done,
                "-" if remaining is None else remaining,
                self.elapsed(),
                self.limit_s(phase),
            )
        )

    def stopped(self, phase: str) -> bool:
        return phase in self._stopped

    def mark(self, phase: str) -> float:
        """Close the current phase; warn when it alone used too much budget."""
        now = self.clock()
        phase_s = now - self._mark
        self._mark = now
        self.timings.append((phase, phase_s))
        if self.budget_s > 0 and phase_s > self.warn_share * self.budget_s:
            self._log(
                "PHASE_BUDGET_WARN|%s|%s|phase_s=%.1f|share=%.2f|warn_share=%.2f|budget_s=%.0f"
                % (
                    self.host,
                    phase,
                    phase_s,
                    phase_s / self.budget_s,
                    self.warn_share,
                    self.budget_s,
                )
            )
        return phase_s

    # -- resume cursors ------------------------------------------------------
    def _cursor_path(self) -> str | None:
        if not self.state_dir:
            return None
        return os.path.join(self.state_dir, "cycle-cursors.json")

    def _read_all(self) -> dict[str, Any]:
        path = self._cursor_path()
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def read_cursor(self, name: str) -> Any:
        return self._read_all().get(name)

    def write_cursor(self, name: str, value: Any) -> None:
        """Persist (or clear, with None) one phase cursor. Best effort, atomic."""
        path = self._cursor_path()
        if not path or not self.persist:
            return
        try:
            data = self._read_all()
            if value is None:
                if name not in data:
                    return
                data.pop(name)
            else:
                if data.get(name) == value:
                    return
                data[name] = value
            os.makedirs(self.state_dir or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".cycle-cursors.")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
            os.replace(tmp, path)
        except OSError:
            pass

    @staticmethod
    def rotate_from(
        items: Sequence[Any], cursor: Any, key: Callable[[Any], Any] = lambda item: item
    ) -> list[Any]:
        """Start ``items`` at the first whose key equals ``cursor`` (wrap around).

        An unknown cursor (item gone) leaves the order unchanged.
        """
        if cursor is None:
            return list(items)
        for index, item in enumerate(items):
            if key(item) == cursor:
                return list(items[index:]) + list(items[:index])
        return list(items)

    def run_steps(self, phase: str, steps: Iterable[tuple[str, Callable[[], Any]]]) -> int:
        """Run named sweep steps in order, stopping at the phase soft deadline.

        The first step not run becomes the cursor; the next cycle starts there,
        so a slow early sweep cannot starve the later ones. At least one step
        always runs. Returns the number of steps run.
        """
        steps = list(steps)
        names = [name for name, _ in steps]
        ordered = self.rotate_from(steps, self.read_cursor(phase), key=lambda s: s[0])
        done = 0
        for name, step in ordered:
            if self.stop(phase, done, len(ordered) - done, min_done=1):
                self.write_cursor(phase, name)
                self._log(
                    "PHASE_DEADLINE_SKIPPED|%s|%s|next=%s|steps=%s"
                    % (self.host, phase, name, ",".join(names))
                )
                return done
            step()
            done += 1
        self.write_cursor(phase, None)
        return done
