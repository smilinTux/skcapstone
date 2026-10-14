"""Recover the recipe actually proven by an operator's acceptance qualification."""

from pathlib import Path

from . import production_test_plan as plan


def executed_profile(home, binding, current, receipt_sha256):
    """Never publish an old recipe against a different recipe's passing receipt."""
    try:
        sealed, _, digest = plan.load_plan(home, binding, allow_completed=True)
    except FileNotFoundError:
        return current
    proposed = sealed.get("profile") or current
    if proposed.get("recipe") == current.get("recipe"):
        return current
    if not (
        sealed.get("initial_profile_qualification") is True
        and sealed.get("acceptance_requalification") is True
    ):
        raise plan.TestEvidenceError("unproven acceptance recipe successor")
    if any(
        proposed.get(key) != current.get(key) for key in ("card", "repository", "criteria_sha256")
    ):
        raise plan.TestEvidenceError("acceptance recipe contract changed")
    # The draft profile was not qualified when sealed. Independently validate
    # the completed run before allowing its recipe into the profile chain.
    from .production_tests import validate_test_receipt

    launch = plan.read_json(plan.run_directory(home, digest) / "launch.json")
    receipt = validate_test_receipt(home, binding, Path(launch["workspace"]))
    if receipt["receipt_sha256"] != receipt_sha256:
        raise plan.TestEvidenceError("acceptance recipe receipt changed")
    return proposed
