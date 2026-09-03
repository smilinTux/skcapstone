"""The review backlog contains live candidates, not historical outcomes."""

import ast
import collections
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _classifier():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "outcome_lifecycle_bucket"
    )
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["outcome_lifecycle_bucket"]


def test_exact_lifecycle_bucket_counts_include_ambiguous() -> None:
    classify = _classifier()
    observations = [
        ("open", True),
        ("claimed", True),
        ("complete", True),
        ("void", True),
        ("ambiguous", True),
        ("claimed", False),
        ("complete", False),
    ]

    assert collections.Counter(classify(*row) for row in observations) == {
        "open": 1,
        "historical_review_claimed": 1,
        "historical_review_terminal": 2,
        "ambiguous": 1,
        "claimed": 1,
        "terminal": 1,
    }


def test_authoritative_lifecycle_is_folded_before_outcome_buckets() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    helper = source.index("def _legacy_selector_decision")
    lifecycle = source.index("lifecycle = lifecycle_state(cid)", helper)
    outcome = source.index("outcome_bucket = outcome_lifecycle_bucket", lifecycle)
    backoff = source.index("if blocked_backoff(cid):", outcome)
    claimability = source.index("decision=authoritative_claimability(cid,core)", backoff)

    assert lifecycle < outcome < backoff < claimability
    assert 'if outcome_bucket != "open":' in source[outcome:backoff]
    assert 'if outcome_bucket == "ambiguous":' in source[outcome:backoff]
    assert 'legacy_reason in {"claimed", "historical_review_claimed"}' in source
    assert "historical_review_terminal += int(" in source
    assert "historical_review_claimed += int(" in source


def test_terminal_review_verdict_remains_excluded_after_lifecycle_fold() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    core_read = source.index("with open(core_p", source.index("def _legacy_selector_decision"))
    terminal_review = source.index('if workflow_class == "terminal_review":', core_read)
    backoff = source.index("if blocked_backoff(cid):", terminal_review)
    pool_append = source.index("pool.append", terminal_review)

    assert core_read < terminal_review < backoff < pool_append
