"""Regression tests for the authoritative terminal-review admission skip.

The scheduler avoids a redundant POOL_V2 admission fold for structurally
labelled review cards only when an authoritative read already proves the card
terminal. These tests pin that contract:

* terminal review cards are skipped (bounded call-count evidence);
* reopened cards and active/claimed review cards are still folded;
* ``initial_labels`` alone is never treated as current terminal authority.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

_HELPERS = {"_reopened_after", "authoritatively_terminal"}


def _load_helpers() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _HELPERS
    }
    assert set(nodes) == _HELPERS
    namespace: dict[str, object] = {
        "re": re,
        "_ts_epoch": lambda value: _ts_epoch(value),
        "event_rows": lambda _cid: [],
        "_load_outcomes": lambda: {},
        "folded_labels": lambda _cid, _core: [],
        "lifecycle_state": lambda _cid: "open",
        "itil_terminal": lambda _cid: False,
        "terminal_review_verdict": lambda _cid, _core: False,
    }
    module = ast.Module(body=list(nodes.values()), type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def _ts_epoch(value: object) -> int:
    text = str(value or "")
    if not text:
        return 0
    # RFC3339 UTC is the only shape produced by the CardStore.
    from datetime import datetime

    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _terminal_namespace(
    *,
    lifecycle: str = "open",
    itil: bool = False,
    review_verdict: bool = False,
    outcome_ts: str | None = None,
    reopen_ts: str | None = None,
) -> dict[str, object]:
    namespace = _load_helpers()
    events = []
    if reopen_ts:
        events.append({"action": "reopen", "ts": reopen_ts})
    namespace["event_rows"] = lambda _cid, _events=events: list(_events)
    outcomes = {}
    if review_verdict:
        outcomes["deadbeef"] = (outcome_ts, "PASS_FOR_REVIEW")
    namespace["_load_outcomes"] = lambda _outcomes=outcomes: dict(_outcomes)
    namespace["folded_labels"] = lambda _cid, _core: ["review"]
    namespace["lifecycle_state"] = lambda _cid, _state=lifecycle: _state
    namespace["itil_terminal"] = lambda _cid, _flag=itil: _flag
    namespace["terminal_review_verdict"] = lambda _cid, _core, _flag=review_verdict: _flag
    return namespace


def test_terminal_cardstore_review_is_authoritatively_terminal() -> None:
    namespace = _terminal_namespace(lifecycle="complete")
    assert namespace["authoritatively_terminal"]("deadbeef", {"id": "deadbeef"}) is True
    namespace = _terminal_namespace(lifecycle="void")
    assert namespace["authoritatively_terminal"]("deadbeef", {"id": "deadbeef"}) is True


def test_terminal_itil_review_is_authoritatively_terminal() -> None:
    namespace = _terminal_namespace(itil=True)
    assert namespace["authoritatively_terminal"]("deadbeef", {"id": "deadbeef"}) is True


def test_terminal_review_outcome_is_authoritatively_terminal() -> None:
    namespace = _terminal_namespace(
        review_verdict=True, outcome_ts="2026-09-30T00:00:00Z"
    )
    assert namespace["authoritatively_terminal"]("deadbeef", {"id": "deadbeef"}) is True


def test_reopened_review_outcome_is_not_skipped() -> None:
    """A reopen newer than the outcome re-arms the card for a full fold."""
    namespace = _terminal_namespace(
        review_verdict=True,
        outcome_ts="2026-09-30T00:00:00Z",
        reopen_ts="2026-09-30T00:01:00Z",
    )
    assert namespace["authoritatively_terminal"]("deadbeef", {"id": "deadbeef"}) is False


def test_reopen_older_than_outcome_does_not_rearm() -> None:
    namespace = _terminal_namespace(
        review_verdict=True,
        outcome_ts="2026-09-30T00:01:00Z",
        reopen_ts="2026-09-30T00:00:00Z",
    )
    assert namespace["authoritatively_terminal"]("deadbeef", {"id": "deadbeef"}) is True


def test_active_claimed_review_is_not_authoritatively_terminal() -> None:
    """Owner/claim semantics survive: an active review is still folded."""
    namespace = _terminal_namespace(lifecycle="claimed", review_verdict=False)
    assert namespace["authoritatively_terminal"]("deadbeef", {"id": "deadbeef"}) is False


def test_initial_labels_alone_are_not_terminal_authority() -> None:
    """A raw structural review label never proves terminality by itself."""
    namespace = _terminal_namespace(lifecycle="open", review_verdict=False)
    core = {"id": "deadbeef", "initial_labels": ["review"]}
    assert namespace["authoritatively_terminal"]("deadbeef", core) is False


def test_launcher_skips_only_after_authoritative_proof() -> None:
    """The structural admission consults the fold, not initial_labels, and counts skips."""
    source = ROTATE.read_text(encoding="utf-8")
    structural = source.index('for label in _structural_core.get("initial_labels", ())')
    proof = source.index("if authoritatively_terminal(cid, _structural_core):", structural)
    append = source.index("_pool_v2_inputs.append((cid, _structural_core))", proof)
    assert structural < proof < append
    assert "_terminal_review_fold_skips += 1" in source
    assert "POOL_V2_TERMINAL_SKIP|%s|structurally_review_terminal_skipped=%d|" in source
    # The legacy selector must run before the structural decision so the
    # decision has the authoritative fold available in the same pass.
    legacy = source.index("legacy = _legacy_selector_decision(cid, core_p)")
    assert legacy < structural
    # The bounded admission fold iterates exactly _pool_v2_inputs, so every
    # skipped card is one avoided authoritative_claimability fold. This ties
    # the counter to the work it measures.
    shadow = source.index("def _shadow_pool_v2():")
    iteration = source.index("for cid, core in _pool_v2_inputs:", shadow)
    fold = source.index("authoritative_claimability(cid, core)", iteration)
    assert shadow < iteration < fold


def test_skip_counter_measurement_shape_is_bounded() -> None:
    """The emitted measurement carries the skip count and bounded input size."""
    source = ROTATE.read_text(encoding="utf-8")
    normalized = source.replace('"\n      "', "")
    line = next(
        row
        for row in normalized.splitlines()
        if row.startswith('log(d,"POOL_V2_TERMINAL_SKIP|')
    )
    assert "structurally_review_terminal_skipped=%d" in line
    assert "pool_v2_inputs=%d" in line
    assert "%(HOST,_terminal_review_fold_skips,len(_pool_v2_inputs))" in line


def test_structural_admission_behaviorally_skips_terminal_and_keeps_reopened() -> None:
    """Drive the exact structural block: terminal skipped, reopened/active folded.

    The block is extracted from the launcher and executed once per synthetic
    card. A terminal review card must not enter ``_pool_v2_inputs``; a reopened
    or actively claimed review card must.
    """
    import textwrap

    source = ROTATE.read_text(encoding="utf-8")
    block = source.split("    legacy = _legacy_selector_decision(cid, core_p)", 1)[1]
    block = block.split("    if lifecycle_state(cid) == \"open\":", 1)[0]
    code = "if True:\n" + textwrap.indent(textwrap.dedent(block), "    ")

    cards = {
        "deadbeef": {"id": "deadbeef", "initial_labels": ["review"]},
        "reopened": {"id": "reopened", "initial_labels": ["review"]},
        "active11": {"id": "active11", "initial_labels": ["review"]},
        "plain123": {"id": "plain123", "initial_labels": []},
    }

    def run(cid: str, terminal: bool) -> dict[str, object]:
        namespace: dict[str, object] = {
            "cid": cid,
            "_structural_core": cards[cid],
            "_legacy_selector_decision": lambda _cid, _p: {
                "reason": "ready",
                "eligible": True,
            },
            "legacy": {"reason": "ready", "eligible": True},
            "authoritatively_terminal": lambda _cid, _core, _t=terminal: _t,
            "_pool_v2_inputs": [],
            "_pool_v2_input_ids": set(),
            "_terminal_review_fold_skips": 0,
            "json": json,
            "os": __import__("os"),
        }
        exec(code, namespace)
        return namespace

    # Terminal review card: skipped, counted, absent from the folded inputs.
    terminal = run("deadbeef", terminal=True)
    assert terminal["_pool_v2_inputs"] == []
    assert terminal["_terminal_review_fold_skips"] == 1

    # Reopened/active review card: still folded into the bounded inputs.
    for cid in ("reopened", "active11"):
        kept = run(cid, terminal=False)
        assert kept["_pool_v2_inputs"] == [(cid, cards[cid])]
        assert kept["_terminal_review_fold_skips"] == 0

    # A structurally non-review card is never added by this block at all.
    plain = run("plain123", terminal=True)
    assert plain["_pool_v2_inputs"] == []
    assert plain["_terminal_review_fold_skips"] == 0


def test_scheduler_facts_preserve_terminal_skip_eligible_set() -> None:
    """Removing a terminal review card cannot remove an eligible card.

    The skip predicate is a subset of the terminal facts POOL_V2 already uses,
    and each of those facts independently forces ineligibility. Reproduce that
    invariant directly against the scheduler classifier.
    """
    from skcapstone.scheduler_decision import SchedulerFacts, classify_scheduler

    for fact in ("terminal_cardstore", "terminal_itil", "selector_excluded"):
        facts = SchedulerFacts(card_id="deadbeef", **{fact: True})
        decision = classify_scheduler(facts)
        assert decision.eligible is False
        assert decision.primary_reason in {
            "terminal_cardstore",
            "terminal_itil",
            "selector_excluded",
        }
