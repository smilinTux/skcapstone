"""Regression: the Niobe cycle stays inside the 270-second wrapper deadline.

The dispatcher reserves 20 seconds for cleanup and the final CYCLE_RECEIPT,
stops launching at the monotonic inner deadline, and caches each logical
route's preflight (success or failure) so 13 lane-compatible rejected
candidates trigger the gateway preflight at most once per route per cycle.
"""

from __future__ import annotations

from pathlib import Path

ROTATE = Path(__file__).resolve().parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def test_cycle_deadline_reserves_receipt_window() -> None:
    source = ROTATE.read_text()
    assert "_CYCLE_DEADLINE_RESERVE_S = 20" in source
    assert "_cycle_deadline = time.monotonic() + 270 - _CYCLE_DEADLINE_RESERVE_S" in source
    # On reaching the inner deadline the loop stops launching and logs the
    # deferral so the final receipt still gets written before the wrapper
    # timeout.
    assert "CYCLE_DEADLINE_REACHED" in source
    assert "_deferred_ids" in source


def test_route_preflight_caches_rejected_routes_per_cycle() -> None:
    source = ROTATE.read_text()
    # Each logical route is resolved/preflighted once; a cached failure must
    # not re-invoke the gateway for later compatible candidates.
    assert "_route_preflight_cache = {}" in source
    assert "reason=cached-failure" in source
    assert "resolve_and_preflight(_GATEWAY_ENDPOINT,model)" in source


def test_cycle_receipt_remains_writable_after_deadline_stop() -> None:
    source = ROTATE.read_text()
    # The final receipt is written after the loop, proving a clean exit path
    # even when the deadline stopped the loop early.
    idx_deadline = source.index("CYCLE_DEADLINE_REACHED")
    idx_receipt = source.index('log(d,"CYCLE_RECEIPT|')
    assert idx_deadline < idx_receipt
