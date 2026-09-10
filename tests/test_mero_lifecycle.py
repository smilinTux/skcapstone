from skcapstone.mero_lifecycle import (
    false_state_launches_zero,
    launch_decision,
    reconcile_snapshot,
    review_dispatch_decision,
)


def row(card_id, **extra):
    return {"card_id": card_id, **extra}


def test_reconcile_same_bounded_snapshot_fails_closed_on_loss_and_gain():
    health = reconcile_snapshot(
        "rev-1",
        {
            "cardstore": [row("a"), row("b")],
            "pool_v2": [row("a"), row("c")],
            "lane": [row("a"), row("c")],
        },
    )
    assert health.stage_counts == {"cardstore": 2, "pool_v2": 2, "lane": 2}
    assert health.invariants["cardstore->pool_v2"] == "BLOCKED"
    assert health.unmatched_ids["cardstore->pool_v2:lost"] == ("b",)
    assert health.unmatched_ids["cardstore->pool_v2:gained"] == ("c",)
    assert health.sha256() and len(health.sha256()) == 64


def test_review_requires_governed_label_producer_hash_and_distinct_reviewer():
    structural = {"claimable": False, "reason": "review"}
    evidence = {"review_label": True, "producer": "link", "evidence_sha256": "a" * 64}
    assert review_dispatch_decision(structural, evidence, reviewer="jarvis") == "PASS_FOR_REVIEW"
    assert (
        review_dispatch_decision(
            structural, {**evidence, "review_label": False}, reviewer="jarvis"
        )
        == "BLOCKED"
    )
    assert review_dispatch_decision(structural, evidence, reviewer="link") == "BLOCKED"
    assert (
        review_dispatch_decision(
            {"claimable": False, "reason": "unknown"}, evidence, reviewer="jarvis"
        )
        == "BLOCKED"
    )
    assert (
        review_dispatch_decision(
            {"claimable": True, "reason": "review"}, evidence, reviewer="jarvis"
        )
        == "BLOCKED"
    )
    assert (
        review_dispatch_decision(structural, {**evidence, "producer": ""}, reviewer="jarvis")
        == "BLOCKED"
    )


def test_false_states_other_than_review_never_launch():
    rows = [
        row("ordinary", claimable=False, reason="unknown"),
        row("stale", claimable=False, reason="stale"),
        row("terminal", claimable=False, reason="terminal"),
        row("human", claimable=False, reason="human"),
        row("sensitive", claimable=False, reason="sensitive"),
        row("superseded", claimable=False, reason="superseded"),
        row("owned", claimable=False, reason="owned"),
        row("malformed", claimable=False, reason="malformed"),
        row("drifted", claimable=False, reason="drifted"),
    ]
    assert false_state_launches_zero(rows)
    assert all(launch_decision(item) == "BLOCKED" for item in rows)


def test_launch_decision_requires_independent_review_evidence():
    structural = {"claimable": False, "reason": "review"}
    evidence = {"review_label": True, "producer": "link", "evidence_sha256": "a" * 64}
    assert launch_decision(structural) == "BLOCKED"
    assert launch_decision(structural, evidence, reviewer="link") == "BLOCKED"
    assert launch_decision(structural, evidence, reviewer="jarvis") == "PASS_FOR_REVIEW"
    assert launch_decision({"claimable": True}, evidence, reviewer="jarvis") == "BLOCKED"


def test_pool_v2_authority_regression_counts_are_not_legacy_ready_count():
    health = reconcile_snapshot(
        "rev-pool-v2",
        {
            "legacy_ready": [row(str(i)) for i in range(8)],
            "pool_v2": [row(str(i)) for i in range(64)],
        },
    )
    assert health.stage_counts == {"legacy_ready": 8, "pool_v2": 64}
    assert health.invariants["legacy_ready->pool_v2"] == "BLOCKED"


def test_unlabeled_review_bypass_is_blocked():
    assert (
        review_dispatch_decision(
            {"claimable": False, "reason": "review"},
            {"producer": "source", "evidence_sha256": "a" * 64},
            reviewer="reviewer",
        )
        == "BLOCKED"
    )
