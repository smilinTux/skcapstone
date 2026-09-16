"""Niobe shadow dispatcher contracts.

This module is deliberately a pure, read-only comparison seam.  It produces
recommendations and handoff evidence but has no lifecycle side effects.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SHADOW_ONLY = "shadow_only"


@dataclass(frozen=True)
class DispatcherObservation:
    """Read-only snapshot used to compare a reference and shadow decision."""

    recommendation_id: str
    claim_revision: str | None
    process_snapshot: Mapping[str, Any]
    evidence_hash: str


@dataclass(frozen=True)
class ParityResult:
    """Deterministic result of comparing two observations."""

    equal: bool
    duplicate: bool
    differences: tuple[str, ...]


def _canonical(value: Any) -> str:
    """Return stable JSON for evidence hashing and comparison."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def evidence_hash(evidence: Mapping[str, Any]) -> str:
    """Hash canonical evidence without mutating the supplied mapping."""
    return hashlib.sha256(_canonical(evidence).encode("utf-8")).hexdigest()


def compare_observations(
    reference: DispatcherObservation, shadow: DispatcherObservation
) -> ParityResult:
    """Compare identity, revision, process state, and evidence hash."""
    fields = (
        "recommendation_id",
        "claim_revision",
        "process_snapshot",
        "evidence_hash",
    )
    differences = tuple(
        field for field in fields if getattr(reference, field) != getattr(shadow, field)
    )
    return ParityResult(
        equal=not differences,
        duplicate=reference == shadow,
        differences=differences,
    )


@dataclass(frozen=True)
class HandoffEvidence:
    """Auditable recommendation, never an authority-bearing activation."""

    card_id: str
    claim_revision: str
    scope: tuple[str, ...]
    expires_at: datetime
    rollback: str
    recommendation: str
    authority: str = "none"
    mode: str = SHADOW_ONLY

    def as_record(self) -> dict[str, Any]:
        """Return stable evidence fields suitable for logging or review."""
        return {
            "card_id": self.card_id,
            "claim_revision": self.claim_revision,
            "scope": list(self.scope),
            "expires_at": self.expires_at.isoformat(),
            "rollback": self.rollback,
            "recommendation": self.recommendation,
            "authority": self.authority,
            "mode": self.mode,
        }


def validate_handoff(
    handoff: HandoffEvidence,
    *,
    observed_revision: str,
    now: datetime,
    active_dispatcher: str | None = None,
) -> tuple[bool, str]:
    """Validate a rehearsal packet while refusing to activate Niobe.

    Exact revision fencing, expiry, replay/duplicate protection, and the
    single-dispatcher rule are checked here.  The function never changes
    dispatcher state.
    """
    if handoff.mode != SHADOW_ONLY or handoff.authority != "none":
        return False, "inactive-seat-denial"
    if handoff.claim_revision != observed_revision:
        return False, "stale-revision"
    if handoff.expires_at <= now:
        return False, "expired"
    if active_dispatcher not in (None, "jarvis"):
        return False, "active-dispatcher-conflict"
    if not handoff.rollback or not handoff.recommendation:
        return False, "incomplete-evidence"
    return True, "shadow-recommendation-only"
