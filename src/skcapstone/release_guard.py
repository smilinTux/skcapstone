"""Content rule for ``coord release-claim``: finished work is not stale.

Incident 2026-10-12 08:46-08:52 UTC: a stale-claim sweeper released 87 claims
with ``--abandon-reason error``. 77 were not stale. The producer had recorded
PASS or PASS_FOR_REVIEW and was correctly holding the claim while independent
review and acceptance ran. Releasing them stranded both.

PR #1166 fences WHO may touch a card's custody. This module is the
complementary rule about WHAT may be released: a claim whose own generation
already carries a producer PASS outcome is refused unless an operator passes an
explicit override with a reason, and a production worker (SKFLEET_CARD_ID set)
can never override.

Scope is the CURRENT claim generation. A reviewer claiming a card that holds an
older producer's PASS has recorded no PASS of its own in this generation, so
review dispatch and review retirement release their own claims unhindered.
Dead-worker recovery that calls ``Board.release_claim`` in process (builder
dispatch, review retire, production exit) does not go through the CLI and is not
changed by this module.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "FinishedWorkReleaseRefusedError",
    "finished_work_verdict",
    "require_release_allowed",
]


class FinishedWorkReleaseRefusedError(ValueError):
    """The claim holder already recorded a PASS for this claim generation."""


def _is_pass(value) -> bool:
    return isinstance(value, str) and value.strip().upper().startswith("PASS")


def finished_work_verdict(
    home: Path, card_id: str, owner: str, claim_revision: str | None
) -> str | None:
    """Return the owner's latest PASS* outcome for this claim generation, else None.

    The latest outcome decides: a PASS superseded by a later BLOCKED or FAIL by
    the same owner in the same generation is no longer finished work.
    """
    from skcoord.card_store import CardStore

    if not claim_revision:
        return None
    store = CardStore(home)
    events = store._read_events(card_id) + store._legacy_events(card_id)
    claim_ts = None
    for event in events:
        if event.get("action") == "claim" and (
            (event.get("claim_revision") or event.get("event_id")) == claim_revision
        ):
            claim_ts = str(event.get("ts", ""))
    if claim_ts is None:
        return None
    outcomes = []
    for event in events:
        if event.get("writer") != owner or str(event.get("ts", "")) < claim_ts:
            continue
        if event.get("expected_claim_revision") not in (None, claim_revision):
            continue
        if event.get("action") == "verdict":
            value = event.get("verdict")
        elif event.get("action") == "link" and str(
            event.get("link_key") or event.get("key") or ""
        ).lower() in {"verdict", "outcome"}:
            value = event.get("link_value") or event.get("value")
        else:
            continue
        outcomes.append((event.get("ts", ""), event.get("seq", 0), str(value or "")))
    if not outcomes:
        return None
    latest = max(outcomes)[2]
    return latest if _is_pass(latest) else None


def require_release_allowed(
    home: Path,
    card_id: str,
    owner: str,
    claim_revision: str | None,
    *,
    override: bool = False,
    reason: str | None = None,
) -> dict | None:
    """Raise unless releasing this claim cannot strand finished work.

    Returns audit fields to record on the release event when an operator
    override was used, else None.
    """
    verdict = finished_work_verdict(home, card_id, owner, claim_revision)
    if verdict is None:
        return None
    if not override:
        raise FinishedWorkReleaseRefusedError(
            f"refusing to release {card_id}: claim holder {owner} recorded latest "
            f"verdict {verdict!r} for this claim and is holding it for review and "
            "acceptance. Finished work is not stale. An operator may pass "
            "--release-finished-work with --reason to override."
        )
    if os.environ.get("SKFLEET_CARD_ID", "").strip():
        raise FinishedWorkReleaseRefusedError(
            f"refusing to release {card_id}: a production worker identity "
            f"(SKFLEET_CARD_ID set) cannot override the finished-work guard "
            f"(latest verdict {verdict!r})."
        )
    if not (reason or "").strip():
        raise FinishedWorkReleaseRefusedError(
            f"--release-finished-work requires a non-empty --reason (latest verdict {verdict!r})."
        )
    return {
        "finished_work_override": True,
        "finished_work_verdict": verdict,
        "finished_work_override_reason": reason.strip(),
    }
