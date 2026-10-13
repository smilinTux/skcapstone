"""Reuse exact released custody and governed test requalification during reseal."""

import json

from skcoord.card_store import CardStore

from ..guarded_released_source import released_view
from .production_review_finish import _finish_test_receipt


def correction_view(home, source, state, claim):
    """Prove custody under the reseal locks; hide only its exact release for diffing."""
    if claim != source["claim"]:
        raise ValueError("reseal custody changed")
    if (
        state["owner"] == source["owner"]
        and state["claim_revision"] == claim
        and state["status"] in {"ready", "doing", "review"}
    ):
        return CardStore(home)
    return released_view(home, source["card"], source["owner"], claim, json.dumps(source))


def validated_acceptance(home, directory, context):
    """Validate current artifacts and the exact bridge to the original finish receipt."""
    from .acceptance_reseal import _validate_artifacts

    return _finish_test_receipt(directory, context, _validate_artifacts(home, context))
