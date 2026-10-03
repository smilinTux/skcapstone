"""Execute-mode mux dispatcher (P4, card c6a87139).

`agent_run.set_execute_dispatcher` is a single module-global seam selected BY
MODE ONLY (execute vs propose/dry-run): there has never been per-surface
routing on it. That was fine while the only execute consumer was code work
(a `repo:<name>`-labeled card, dispatched to the sandboxed
skharness.autocode bridge). It stops being fine once a card whose
`meta.origin.surface` is "alert" or "gtd" reaches execute mode: today that
card has no repo label, so the code bridge refuses it cleanly (a well-formed
refusal, never a crash - verified ground truth for this card) and the card
just dead-ends in NEEDS_REVIEW having done nothing useful, even though
`agent_run.gate()` already has dedicated draft-only gate rows for exactly
these surfaces.

This module is the mux: it folds the card once, reads `meta.origin.surface`,
and routes to the right of two dispatchers. `agent_run._maybe_wire_execute_mux`
is the only caller, wiring the result into `set_execute_dispatcher` itself so
`process_one` needs no awareness that multiple executors exist.

Design doc: docs/specs/2026-08-13-unified-consent-plane-arch.md section 5.2.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("skcapstone.execute_mux")

#: Surfaces this mux treats as ops/comms work rather than code work. GTD
#: next-actions are frequently outbound comms themselves (see
#: agent_run._HEURISTIC_GTD / _SEND_VERBS); alert is this card's new surface.
COMMS_SURFACES = frozenset({"alert", "gtd"})

_NOT_WIRED_SUMMARY = "execute NOT dispatched: sandboxed executor unavailable"
_NOT_WIRED_REASON = (
    "execute gated (R1): requires the sandboxed skharness.autocode "
    "executor; none wired, so it was NOT dispatched. Recording plan only."
)


def _not_wired_result() -> dict:
    """Same shape (and near-identical text) `process_one` used to emit
    itself when `_execute_dispatcher` was `None` outright, so wrapping the
    seam in a mux does not change what a caller with no code bridge wired
    sees for a repo-labeled card."""
    return {
        "summary": _NOT_WIRED_SUMMARY,
        "activity": [{"atype": "elicitation", "text": _NOT_WIRED_REASON}],
        "links": {},
    }


def _card_routing(home: Path, card_id: Optional[str]) -> tuple[bool, Optional[str]]:
    """Return ``(has_repo_label, origin_surface)`` for ``card_id``.

    Never raises: a fold failure (missing card, corrupt store, blank id) is
    treated exactly like a card with no repo label and no known origin,
    which routes to the code bridge leg - fail-closed by falling back to
    today's behavior, never fail-crash.
    """
    if not card_id:
        return False, None
    try:
        from .card_store import CardStore

        card = CardStore(home).fold(card_id)
    except Exception as exc:  # noqa: BLE001 - routing must never crash a run
        logger.info("execute_mux: could not fold %s for routing: %s", card_id, exc)
        return False, None
    if card is None:
        return False, None
    labels = getattr(card, "labels", None) or []
    has_repo_label = any(str(label).startswith("repo:") for label in labels)
    origin = (getattr(card, "meta", None) or {}).get("origin") or {}
    return has_repo_label, origin.get("surface")


def build_execute_mux(
    home: Path,
    code_dispatcher: Optional[Callable[[dict], dict]],
    comms_dispatcher: Optional[Callable[[dict], dict]],
) -> Callable[[dict], dict]:
    """Build a single ``fn(context) -> {"summary","activity","links"}``
    suitable for ``agent_run.set_execute_dispatcher``.

    Routing:
        - A `repo:<name>`-labeled card is code work: always routed to
          ``code_dispatcher``, unconditionally taking priority over surface
          routing (a card can carry both a repo label and an origin
          surface; an explicit repo label is the stronger, more specific
          signal that this is code work).
        - Anything else whose `meta.origin.surface` is in ``COMMS_SURFACES``
          routes to ``comms_dispatcher``.
        - Everything else (including a fold failure) falls through to
          ``code_dispatcher``, preserving today's behavior for every card
          this mux does not specifically re-route.

    Either dispatcher may be ``None`` (not wired). The mux never calls a
    ``None`` dispatcher: it returns ``_not_wired_result()`` instead, exactly
    mirroring what ``process_one`` used to do itself when
    ``_execute_dispatcher`` was ``None``.
    """

    def mux(context: dict) -> dict:
        card_id = context.get("card_id")
        has_repo_label, surface = _card_routing(home, card_id)
        if not has_repo_label and surface in COMMS_SURFACES:
            if comms_dispatcher is None:
                return _not_wired_result()
            return comms_dispatcher(context)
        if code_dispatcher is None:
            return _not_wired_result()
        return code_dispatcher(context)

    mux._is_execute_mux = True  # idempotency marker for _maybe_wire_execute_mux
    return mux
