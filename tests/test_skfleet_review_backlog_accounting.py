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
    backoff = source.index("if blocked_backoff(cid):")
    terminal_review = source.index("if terminal_review_verdict(cid, core):", backoff)
    pool_append = source.index("pool.append", terminal_review)

    assert backoff < terminal_review < pool_append


def test_governed_review_is_the_only_executable_review_state() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    reason = source[
        source.index("def _claimability_reason") : source.index("def _authoritative_card_state")
    ]
    decision = source[
        source.index("def authoritative_claimability") : source.index("def lifecycle_state")
    ]

    assert 'state["status"] == "review"' in reason
    assert '"review" in normalized_labels' in reason
    assert "not (_NOT_CLAIMABLE & normalized_labels)" in reason
    assert 'return "governed-review"' in reason
    assert 'reason in {"claimable", "governed-review"}' in decision
    assert reason.index('return "governed-review"') < reason.index('return "review"')


def test_governed_review_still_passes_link_before_claim() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    launch = source[source.index("for _pick_index,(_LANE,") :]

    assignment = launch.index("_pool_v2_preclaim_handoff(")
    claim = launch.index('claim=subprocess.run([SKC,"coord","claim"')
    assert assignment < claim


def test_review_admission_uses_same_snapshot_fence_as_hash_partition() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    authority = source[
        source.index("def _pool_v2_authority_rows") : source.index("# Partition the CARD SPACE")
    ]
    preclaim = source[
        source.index("def _pool_v2_preclaim_matches") : source.index("def _pool_v2_overlay")
    ]
    launch = source[source.index("# Last-moment re-check") :]

    assert "_POOL_V2_ADMISSIONS" in authority
    assert "_pool_v2_fingerprint(fresh) == _pool_v2_fingerprint(selected)" in preclaim
    assert (
        "fresh_claimability=authoritative_claimability(cid,core=_fresh_core,fresh=True)" in launch
    )
    assert "_pool_v2_preclaim_handoff(" in launch
    assert "SKIPPED_ADMISSION_DRIFT" in launch
