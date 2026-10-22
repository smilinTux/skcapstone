"""Validate attributed recovery evidence without granting release authority."""

import hashlib
import json
from pathlib import Path

from .fleet.production_policy import _unique_object
from .fleet.production_test_plan import read_private


def recovery_evidence(path: Path, *, card: str, owner: str, revision: str, actor: str) -> dict:
    """Bind explicit operator confirmation and preserved bytes to one claim.

    The caller still enforces normal actor authorization and the current claim
    under its mutation lock. This receipt is evidence, never an authority token.
    """
    if not path.is_absolute():
        raise ValueError("recovery receipt path must be absolute")
    raw = read_private(path, maximum=65536)
    data = json.loads(raw, object_pairs_hook=_unique_object)
    expected = dict(
        schema="skcapstone.operator-recovery/v1",
        card_id=card, owner=owner, claim_revision=revision, controller=actor,
        authority="operator-attestation",
    )
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in expected.items()):
        raise ValueError("recovery receipt identity or authority mismatch")
    if data.get("workers_stopped") is not True:
        raise ValueError("recovery requires explicit stopped-worker confirmation")
    statement = data.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("recovery requires the operator statement")
    custody = data.get("custody")
    if not isinstance(custody, list) or not 1 <= len(custody) <= 16:
        raise ValueError("recovery requires 1 to 16 preserved custody artifacts")
    for item in custody:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("invalid recovery custody artifact")
        artifact = Path(item["path"])
        if not artifact.is_absolute():
            raise ValueError("recovery custody path must be absolute")
        digest = hashlib.sha256(read_private(artifact)).hexdigest()
        if digest != item.get("sha256"):
            raise ValueError("recovery custody digest mismatch")
    return dict(recovery_receipt=str(path), recovery_receipt_sha256=hashlib.sha256(raw).hexdigest())
