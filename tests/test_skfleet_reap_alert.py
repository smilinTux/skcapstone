"""Alert when reaping silently leaves lifecycle stale claims in place."""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_state_transition():
    """Load the pure alert state transition without executing fleet rotation."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_next_reap_alert_state"
    )
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["_next_reap_alert_state"]


def test_alerts_once_after_three_zero_release_cycles_with_stale_claims() -> None:
    """The third consecutive ineffective cycle names both lifecycle classes."""
    transition = _load_state_transition()
    lifecycle = {
        "counts": {"stale_claims": 1, "dead_worker_claims": 1},
        "classes": {
            "stale_claims": [{"card_id": "bbbbbbbb"}],
            "dead_worker_claims": [{"card_id": "aaaaaaaa"}],
        },
    }

    state = {}
    state, alert = transition(state, 0, lifecycle)
    assert alert == []
    state, alert = transition(state, 0, lifecycle)
    assert alert == []
    state, alert = transition(state, 0, lifecycle)
    assert alert == ["aaaaaaaa", "bbbbbbbb"]
    state, alert = transition(state, 0, lifecycle)
    assert alert == []


def test_successful_release_resets_consecutive_cycle_state() -> None:
    """A release breaks the consecutive silent no-op sequence."""
    transition = _load_state_transition()
    lifecycle = {
        "counts": {"stale_claims": 1, "dead_worker_claims": 0},
        "classes": {"stale_claims": [{"card_id": "aaaaaaaa"}]},
    }

    state, _ = transition({}, 0, lifecycle)
    state, alert = transition(state, 1, lifecycle)

    assert alert == []
    assert state == {"consecutive_cycles": 0, "alerted": False, "card_ids": []}
