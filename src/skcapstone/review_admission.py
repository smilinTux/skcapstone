"""Shared, read-only governed-review admission diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


def governed_review_metadata(
    core: Mapping[str, object], labels: Sequence[str]
) -> tuple[str, str] | None:
    """Return typed producer evidence for an explicitly labelled review."""
    normalized = {str(label).strip().lower() for label in labels}
    if "review" not in normalized:
        return None
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    producer = str(links.get("producer_identity") or meta.get("producer_identity") or "").strip()
    evidence = (
        str(links.get("candidate_evidence_sha256") or meta.get("candidate_evidence_sha256") or "")
        .strip()
        .lower()
    )
    if producer or evidence:
        return (
            (producer, evidence) if producer and re.fullmatch(r"[0-9a-f]{64}", evidence) else None
        )
    description = str(core.get("description") or "")
    producer_match = re.search(r"Producer identity:\s*([^.]*)\.", description)
    evidence_match = re.search(r"sha256=([0-9a-f]{64})(?:\.|\s|$)", description)
    if not producer_match or not producer_match.group(1).strip() or not evidence_match:
        return None
    return producer_match.group(1).strip(), evidence_match.group(1)


def governed_review_gate_reasons(
    core: Mapping[str, object],
    labels: Sequence[str],
    *,
    dependency_blocked: bool = False,
    owned: bool = False,
    capacity_available: bool = True,
) -> tuple[str, ...]:
    """Return stable reason codes used by CLI and POOL_V2 diagnostics."""
    normalized = {str(label).strip().lower() for label in labels}
    if "review" not in normalized:
        return ()
    reasons = []
    if "seat-seraph" not in normalized:
        reasons.append("wrong-seat")
    if governed_review_metadata(core, labels) is None:
        reasons.append("absent-typed-metadata")
    if dependency_blocked:
        reasons.append("dependency")
    if owned:
        reasons.append("ownership")
    if not capacity_available:
        reasons.append("capacity")
    return tuple(reasons)
