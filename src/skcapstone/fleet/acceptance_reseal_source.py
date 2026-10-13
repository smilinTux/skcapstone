"""Reuse exact released custody and governed test requalification during reseal."""

from .production_review_finish import _finish_test_receipt
from .source_custody import CustodyState, inspect_custody


def correction_view(home, source, state, claim):
    """Prove custody under the reseal locks; hide only its exact release for diffing."""
    if claim != source["claim"]:
        raise ValueError("reseal custody changed")
    proof = inspect_custody(home, source["card"], source["owner"], claim, source=source)
    if proof.state == CustodyState.HELD:
        return proof.store
    if proof.state != CustodyState.RELEASED_CLEAN:
        raise ValueError("reseal custody changed")
    return proof.before_release()


def validated_acceptance(home, directory, context):
    """Validate current artifacts and the exact bridge to the original finish receipt."""
    from .acceptance_reseal import _validate_artifacts

    return _finish_test_receipt(directory, context, _validate_artifacts(home, context))
