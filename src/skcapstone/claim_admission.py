"""Lock-bound admission for coordination claims."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def assert_claim_admitted(home: Path, task_id: str, agent_name: str) -> None:
    """Apply every SKCapstone claim gate to one current card generation.

    Args:
        home: Coordination home containing the CardStore.
        task_id: Exact card being claimed.
        agent_name: Canonical claimant identity.
    """
    from .fleet.churn_breaker import assert_claim_permitted
    from .human_wait import assert_human_claim
    from .review_admission import assert_governed_review_claim

    assert_human_claim(home, task_id, agent_name)
    assert_governed_review_claim(home, task_id, agent_name)
    assert_claim_permitted(home, task_id, agent_name)


def claim_task_with_locked_admission(
    board: Any,
    agent_name: str,
    task_id: str,
    *,
    force: bool = False,
) -> Any:
    """Claim only after rerunning admission under Board's card lock.

    Args:
        board: The ``skcoord.coordination.Board`` instance performing the claim.
        agent_name: Canonical claimant identity.
        task_id: Exact card being claimed.
        force: Compatibility flag forwarded to ``Board.claim_task``.

    Returns:
        The agent projection returned by ``Board.claim_task``.
    """
    original = board._claim_task

    def guarded(agent: str, card_id: str, claimed_force: bool = False, **kwargs: Any) -> Any:
        if card_id == task_id:
            assert_claim_admitted(Path(board.home), card_id, agent)
        return original(agent, card_id, claimed_force, **kwargs)

    board._claim_task = guarded
    try:
        return board.claim_task(agent_name, task_id, force=force)
    finally:
        board._claim_task = original
