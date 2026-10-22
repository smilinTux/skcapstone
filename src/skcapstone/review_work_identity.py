"""Stable source generation and deterministic native review identities."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def card_generation(card: Any) -> str:
    """Return the canonical Link generation for one folded source card."""

    def value(name: str, default: Any = None) -> Any:
        return card.get(name, default) if isinstance(card, dict) else getattr(card, name, default)

    links = value("links", {}) or {}
    status = value("status")
    stable = {
        "id": value("id"),
        "status": getattr(status, "value", status),
        "owner": value("owner"),
        "labels": sorted(value("labels", []) or []),
        "dependencies": sorted(value("dependencies", []) or []),
        "verdict": str(links.get("verdict") or links.get("outcome") or "").strip().upper(),
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def review_card_id(
    source_card: str,
    head_revision: str,
    card_generation: str,
    evidence_sha256: str,
    review_class: str = "review",
    *,
    review_attempt: str = "",
) -> str:
    """Keep attempt-zero identity unchanged; bind replacement to authorization."""
    identity = "\0".join(
        (source_card, head_revision, card_generation, evidence_sha256, review_class)
    )
    if review_attempt:
        if not re.fullmatch(r"[0-9a-f]{64}", review_attempt):
            raise ValueError("review replacement attempt invalid")
        identity += "\0replacement\0" + review_attempt
    return hashlib.sha256(identity.encode()).hexdigest()[:8]
