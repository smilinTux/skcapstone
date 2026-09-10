"""Shared, read-only governed-review admission diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path


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


def governed_review_source_binding(
    core: Mapping[str, object], labels: Sequence[str]
) -> tuple[str, str] | None:
    """Return the immutable source card and revision for Seraph review work."""
    if "review" not in {str(label).strip().lower() for label in labels}:
        return None
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    source = str(links.get("link_source_card") or meta.get("link_source_card") or "").strip()
    head = str(links.get("link_head_revision") or meta.get("link_head_revision") or "").strip()
    if not source or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    return source, head


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
    if governed_review_source_binding(core, labels) is None:
        reasons.append("absent-source-binding")
    if dependency_blocked:
        reasons.append("dependency")
    if owned:
        reasons.append("ownership")
    if not capacity_available:
        reasons.append("capacity")
    return tuple(reasons)


def assert_governed_review_claim(home: Path, card_id: str, agent: str) -> None:
    """Fail closed when a SKCapstone claim bypasses governed review admission."""
    from .card_store import CardStore

    card = CardStore(home).fold(card_id)
    if card is None:
        return
    labels = [str(label).strip().lower() for label in card.labels]
    if "review" not in labels:
        return
    core = {
        "title": card.title,
        "description": card.description,
        "links": card.links,
        "meta": card.meta,
    }
    reasons = governed_review_gate_reasons(core, labels)
    reviewer = agent.strip().lower()
    elastic = re.fullmatch(
        rf"pi-codex-review-[a-z0-9][a-z0-9-]*-{re.escape(card_id.lower())}", reviewer
    )
    if reviewer != "seraph" and not reviewer.startswith("pi-seraph-") and not elastic:
        reasons = ("wrong-reviewer", *reasons)
    if reasons:
        raise ValueError("governed review claim denied: " + ", ".join(dict.fromkeys(reasons)))
