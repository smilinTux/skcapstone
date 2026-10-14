"""Explicit operator correction of one source-only receipt's source-head typo."""

import hashlib
import json
import re

from skcoord.card_store import CardStore

KEY = "applicability_receipt_correction"
ACTOR = "lumina-nor"


def receipt_digest(receipt):
    return hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def corrected_receipt(home, card, receipt, expected_head, events):
    """Preserve reviewer attribution; apply only an exact, separately audited correction."""
    if receipt.get("source_head") == expected_head:
        return receipt
    corrections = [e for e in events if (e.get("link_key") or e.get("key")) == KEY]
    if not corrections:
        return receipt
    row = CardStore(home).fold(card)
    if row is None or row.archived or row.owner != receipt.get("reviewer"):
        raise ValueError("correction review custody changed")
    matching = []
    for event in corrections:
        value = json.loads(event.get("link_value", event.get("value", "")))
        if not isinstance(value, dict):
            raise ValueError("correction is not an object")
        # Corrections for another generation never authorize this receipt.
        if value.get("claim_revision") != row.meta.get("_claim_revision"):
            continue
        if event.get("writer") != ACTOR:
            raise ValueError("correction actor differs")
        if set(value) != {
            "schema",
            "card",
            "claim_revision",
            "receipt_sha256",
            "source_head",
            "review_head",
            "decision",
            "reason",
        }:
            raise ValueError("correction shape differs")
        decision = value["decision"]
        if (
            value["schema"] != "skfleet.applicability-head-correction/v1"
            or value["card"] != card
            or value["receipt_sha256"] != receipt_digest(receipt)
            or value["source_head"] != expected_head
            or value["review_head"] != receipt.get("source_head")
            or not re.fullmatch(r"[0-9a-f]{40}", str(value["review_head"]))
            or value["review_head"] == expected_head
            or not isinstance(value["reason"], str)
            or not value["reason"].strip()
            or not isinstance(decision, dict)
            or decision.get("schema") != "skfleet.source-review-decision/v1"
            or decision.get("card") != card
            or decision.get("parent_card") != row.meta.get("link_source_card")
            or decision.get("reviewer_identity") != receipt.get("reviewer")
            or decision.get("source_head") != expected_head
            or decision.get("source_tree") != row.meta.get("candidate_tree")
            or decision.get("verdict") != "PASS"
            or decision.get("report_sha256") != receipt.get("evidence_digest")
        ):
            raise ValueError("correction binding differs")
        matching.append(value)
    if not matching:
        return receipt
    if any(v != matching[0] for v in matching[1:]):
        raise ValueError("correction conflicts")
    return dict(receipt, source_head=expected_head)
