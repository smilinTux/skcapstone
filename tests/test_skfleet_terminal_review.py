"""Terminal independent-review verdicts must not be dispatched again."""

import ast
from pathlib import Path

from skcapstone.review_admission import review_generation_eligibility

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(labels, outcomes):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "terminal_review_verdict"
    )
    event = {
        "ts": outcomes[0],
        "writer": "reviewer",
        "seq": 1,
        "action": "link",
        "link_key": "verdict",
        "link_value": outcomes[1],
    }
    namespace = {
        "folded_labels": lambda cid, core: labels,
        "_outcome_scan_rows": lambda cid: [event],
        "review_generation_eligibility": review_generation_eligibility,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    core = {
        "meta": {
            "producer_identity": "producer",
            "candidate_evidence_sha256": "a" * 64,
            "link_source_card": "source01",
            "link_head_revision": "b" * 40,
        }
    }
    return namespace["terminal_review_verdict"]("card", core)


def test_terminal_review_outcomes_are_not_dispatched_again():
    for verdict in (
        "PASS",
        "FAIL_FOR_REPAIR",
        "FAIL_CLOSED",
        "FAIL_ROLLED_BACK",
        "BLOCKED blocked_on=capability referent=ac:1",
    ):
        assert _load(["review"], ("2026-09-01T14:28:05Z", verdict))


def test_review_fail_lifecycle_disposition_is_terminal():
    assert _load(
        ["review"],
        (
            "2026-09-15T23:12:01Z",
            "FAIL_EVIDENCE_RECOVERED_RELEASE_EXACT_GENERATION_TO_REVIEW",
        ),
    )


def test_source_pass_for_review_is_not_terminal():
    assert not _load(["repair"], ("2026-09-01T14:28:05Z", "PASS_FOR_REVIEW"))
