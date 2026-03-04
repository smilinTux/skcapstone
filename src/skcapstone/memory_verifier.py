"""
Two-factor memory verification — truth-check gate for short-term → mid-term promotion.

Before any SHORT_TERM → MID_TERM promotion this module:
  1. Calls skseed.skill.truth_check() on the candidate memory content.
  2. If a contradiction is found (coherence_score < 0.7 or collision_fragments non-empty):
     a. Tags the candidate memory with tag=conflicting (saves it back to short-term).
     b. Stores a conflict-report memory (also tag=conflicting) documenting the issue.
     c. Fires a desktop notification with urgency=critical.
     d. Returns VerificationResult(should_promote=False).
  3. If truth-aligned, returns VerificationResult(should_promote=True).

Promotion is skipped until the conflict is resolved — i.e., the ``conflicting`` tag
is manually cleared (or via skseed_audit resolution).

Fail-open design: if skseed is not installed, or truth_check raises unexpectedly,
promotion proceeds normally so minimal deployments are unaffected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.memory_verifier")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """Result of a pre-promotion truth-check gate.

    Attributes:
        should_promote: Whether the memory may advance to mid-term.
        is_conflicting: True when a contradiction was detected.
        coherence_score: Collider coherence score (0.0–1.0).
        collision_fragments: Contradiction strings found by the collider.
        truth_grade: TruthGrade value string from the collider.
        conflict_report_id: memory_id of the stored conflict-report entry,
            present only when is_conflicting is True.
    """

    should_promote: bool
    is_conflicting: bool = False
    coherence_score: float = 0.0
    collision_fragments: list[str] = field(default_factory=list)
    truth_grade: str = "ungraded"
    conflict_report_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Public gate
# ---------------------------------------------------------------------------


def verify_before_promotion(home: Path, entry) -> VerificationResult:
    """Truth-check gate called before promoting a SHORT_TERM memory to MID_TERM.

    Args:
        home: Agent home directory (~/.skcapstone).
        entry: MemoryEntry candidate (expected to be in SHORT_TERM layer).

    Returns:
        VerificationResult — consult .should_promote before proceeding.
    """
    # Only gate SHORT_TERM → MID_TERM transitions
    from .models import MemoryLayer

    if entry.layer != MemoryLayer.SHORT_TERM:
        return VerificationResult(should_promote=True)

    # Skip the gate for verifier-generated meta-records (conflict reports).
    # These entries describe the conflict itself and must not be re-checked,
    # which would create an infinite verification cascade.
    if getattr(entry, "source", "") == "memory_verifier":
        return VerificationResult(should_promote=True)

    # Fail-open if skseed is not installed
    try:
        from skseed.skill import truth_check
    except ImportError:
        logger.debug(
            "skseed not installed — skipping truth-check gate (fail-open) for %s",
            entry.memory_id,
        )
        return VerificationResult(should_promote=True)

    try:
        result = truth_check(
            belief=entry.content,
            source="model",
            domain="memory-promotion",
        )
    except Exception as exc:
        logger.warning(
            "truth_check raised for memory %s (fail-open): %s",
            entry.memory_id,
            exc,
        )
        return VerificationResult(should_promote=True)

    is_aligned: bool = result.get("is_aligned", True)
    collider: dict = result.get("collider_result", {})
    fragments: list[str] = collider.get("collision_fragments", [])
    coherence: float = float(collider.get("coherence_score", 1.0))
    grade: str = str(collider.get("truth_grade", "ungraded"))

    contradiction_found = (not is_aligned) or bool(fragments)

    if not contradiction_found:
        logger.debug(
            "Memory %s passed truth-check (coherence=%.2f grade=%s) — promotion allowed",
            entry.memory_id,
            coherence,
            grade,
        )
        return VerificationResult(
            should_promote=True,
            coherence_score=coherence,
            truth_grade=grade,
        )

    # --- Contradiction detected ---
    logger.warning(
        "Memory %s failed truth-check: is_aligned=%s coherence=%.2f grade=%s fragments=%d",
        entry.memory_id,
        is_aligned,
        coherence,
        grade,
        len(fragments),
    )

    _tag_conflicting(home, entry)
    report_id = _store_conflict_report(home, entry, fragments, coherence, grade)
    _fire_conflict_notification(entry, fragments, coherence)

    return VerificationResult(
        should_promote=False,
        is_conflicting=True,
        coherence_score=coherence,
        collision_fragments=fragments,
        truth_grade=grade,
        conflict_report_id=report_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tag_conflicting(home: Path, entry) -> None:
    """Add tag=conflicting to the candidate entry and re-save it to short-term."""
    if "conflicting" not in entry.tags:
        entry.tags = list(entry.tags) + ["conflicting"]
    try:
        from .memory_engine import _save_entry, _update_index

        _save_entry(home, entry)
        _update_index(home, entry)
        logger.info(
            "Tagged memory %s as conflicting (stays in short-term)",
            entry.memory_id,
        )
    except Exception as exc:
        logger.error(
            "Failed to persist conflicting tag for %s: %s",
            entry.memory_id,
            exc,
        )


def _store_conflict_report(
    home: Path,
    entry,
    fragments: list[str],
    coherence: float,
    grade: str,
) -> Optional[str]:
    """Store a short-term conflict-report memory documenting the failed truth-check.

    Returns:
        memory_id of the created report, or None on failure.
    """
    if fragments:
        fragment_summary = "; ".join(fragments[:3])
        if len(fragments) > 3:
            fragment_summary += f" … (+{len(fragments) - 3} more)"
    else:
        fragment_summary = "coherence below threshold"

    preview = entry.content[:120]
    if len(entry.content) > 120:
        preview += "…"

    report_content = (
        f"[CONFLICT REPORT] Memory {entry.memory_id!r} failed truth-check — "
        f"SHORT_TERM→MID_TERM promotion BLOCKED. "
        f"Coherence: {coherence:.2f}, Grade: {grade}. "
        f"Contradictions: {fragment_summary}. "
        f"Content preview: {preview!r}"
    )

    try:
        from .memory_engine import store as mem_store
        from .models import MemoryLayer

        report = mem_store(
            home=home,
            content=report_content,
            tags=["conflicting", "truth-check", "promotion-blocked"],
            source="memory_verifier",
            importance=0.6,   # below 0.7: stays in short-term until conflict resolved
            layer=MemoryLayer.SHORT_TERM,
        )
        logger.info(
            "Stored conflict report %s for candidate %s",
            report.memory_id,
            entry.memory_id,
        )
        return report.memory_id
    except Exception as exc:
        logger.error("Failed to store conflict report for %s: %s", entry.memory_id, exc)
        return None


def _fire_conflict_notification(entry, fragments: list[str], coherence: float) -> None:
    """Dispatch a critical desktop notification for a promotion conflict."""
    preview = entry.content[:60]
    if len(entry.content) > 60:
        preview += "…"

    if fragments:
        hint = f" — {fragments[0][:50]}"
    else:
        hint = f" (coherence {coherence:.2f})"

    title = "Memory Conflict Detected"
    body = f"Promotion blocked [{entry.memory_id}]: {preview}{hint}"

    try:
        from .notifications import notify

        notify(title=title, body=body, urgency="critical")
        logger.info(
            "Fired critical notification for conflicting memory %s",
            entry.memory_id,
        )
    except Exception as exc:
        logger.error(
            "Failed to fire conflict notification for %s: %s",
            entry.memory_id,
            exc,
        )
