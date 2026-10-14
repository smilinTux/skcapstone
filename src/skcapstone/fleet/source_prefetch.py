"""Overlap the network half of requalification offers with the sequential offer loop.

Each requalification offer first rebuilds the card's exact source with a depth-1
``git fetch`` (several seconds, sometimes a timeout). The offer loop is strictly
sequential (one CardStore fence, one claim per card), so a dozen unqualified cards
spent a dozen network round trips back to back: 84 s of one 250 s dispatcher cycle
on 2026-10-14, leaving lane selection and launch no time.

The fetch itself is independent per card. This module starts the fetches for the
cards the loop is about to offer on a few daemon threads, in loop order, and lets
the loop collect each result when it reaches that card. Nothing about the offer
changes: the same function runs with the same arguments, the loop still decides
card by card in the same order, and a fetch that failed re-raises its exact error
at the exact call site that would have raised it. Cards the loop never reaches
cost one wasted fetch into a workspace the next cycle reuses.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from typing import Any

DEFAULT_WORKERS = 4


class SourcePrefetch:
    """Run ``prepare(core, labels)`` for submitted cards on daemon threads."""

    def __init__(
        self, prepare: Callable[[dict, Any], Any], *, workers: int = DEFAULT_WORKERS
    ) -> None:
        self._prepare = prepare
        self._workers = max(1, int(workers))
        self._queue: queue.Queue = queue.Queue()
        self._futures: dict[str, tuple[Future, dict, Any]] = {}
        self._started = 0
        self._lock = threading.Lock()

    def submit(self, core: dict, labels: Any) -> bool:
        """Queue one card; False when it was already queued."""
        card_id = str(core.get("id") or "")
        with self._lock:
            if not card_id or card_id in self._futures:
                return False
            future: Future = Future()
            self._futures[card_id] = (future, core, labels)
            self._queue.put((future, core, labels))
            if self._started < self._workers:
                self._started += 1
                threading.Thread(target=self._run, name="source-prefetch", daemon=True).start()
        return True

    def _run(self) -> None:
        while True:
            try:
                future, core, labels = self._queue.get_nowait()
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._started -= 1
                        return
                continue
            try:
                future.set_result(self._prepare(core, labels))
            except BaseException as exc:  # noqa: BLE001 - replayed at the call site
                future.set_exception(exc)

    def prepare(self, core: dict, labels: Any) -> Any:
        """Drop-in for the ``prepare_workspace`` callback.

        Returns (or raises) what the prefetch produced when it was started for this
        exact ``core`` and ``labels``; otherwise runs the work inline, so a core
        that changed since the prediction is never answered from stale input.
        """
        card_id = str(core.get("id") or "")
        with self._lock:
            queued = self._futures.pop(card_id, None)
        if queued is None or queued[1] != core or queued[2] != labels:
            return self._prepare(core, labels)
        return queued[0].result()

    def pending(self) -> int:
        with self._lock:
            return sum(1 for future, _core, _labels in self._futures.values() if not future.done())


def predict_requalifications(
    candidates: Iterable[tuple],
    *,
    needs_requalification: Callable[[dict, Any], bool],
    folded_core: Callable[[dict], dict | None],
    limit: int,
) -> list[tuple[dict, Any]]:
    """Return ``(core, labels)`` for the first ``limit`` cards that will need a fetch.

    ``candidates`` are the offer loop's rows ``(.., .., card_id, core, labels)`` in
    loop order. ``needs_requalification`` is the cheap, read-only profile check that
    makes ``offer`` raise ``required-test-profile-unqualified``; ``folded_core``
    returns the core ``offer_stale_candidate`` will pass to the workspace callback
    (or None when that function would defer before touching the source).
    """
    chosen: list[tuple[dict, Any]] = []
    for row in candidates:
        if len(chosen) >= limit:
            break
        card_id, core, labels = row[2], row[3], row[4]
        try:
            if not needs_requalification(dict(core, id=card_id), labels):
                continue
            merged = folded_core(dict(core, id=card_id))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if merged is not None:
            chosen.append((merged, labels))
    return chosen
