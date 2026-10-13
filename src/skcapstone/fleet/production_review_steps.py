"""Deterministic native acceptance steps retained by finish and recovery journals."""

import json


def steps(context, acceptance, *, source_claim_current=True):
    """Complete reviewer first; source completion joins its exact final revision."""
    source, review = context["source"], context["review"]
    audit = json.dumps(acceptance, sort_keys=True, separators=(",", ":"))
    # The reviewer has already sealed exactly one applicability receipt after
    # its report and verdict. Reissuing those links would invalidate its order
    # or create a duplicate receipt. Add only independent controller test proof.
    values = [("test_acceptance", audit)]
    result = [("review", "link", key, value) for key, value in values]
    result.append(("review", "complete", "", ""))
    repo = source["repository"].rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    values = [
        ("review_join", review["card"]),
        ("test_acceptance", audit),
        ("commit_sha", source["head"]),
        ("branch", repo + ":" + source["ref"].removeprefix("refs/heads/")),
        ("head", source["head"]),
        ("candidate_evidence_sha256", source["evidence_sha256"]),
        ("verdict", "PASS"),
        ("evidence", source["evidence_path"]),
        ("evidence_sha256", source["evidence_sha256"]),
    ]
    if source_claim_current:
        result.extend(("source", "link", key, value) for key, value in values)
    result.append(("source", "complete", "", ""))
    return result
