from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.skrsi_estate_adapters import (
    ESTATE_HANDOFFS,
    METRIC_EVENTS,
    AuthoritySnapshot,
    AuthorityUnavailableError,
    BoundedFleetFanout,
    EligibleWork,
    FanoutBudget,
    LIFECYCLE_SEATS,
    ReviewHandoff,
    append_metric_event,
    canonical_review_card,
)
from skcapstone.skrsi_registry import AppendOnlyOutbox, SKRSIError

NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def eligible(key, seat="seat-a", host="host-a", evaluator="seraph", route="review", **changes):
    values = dict(
        natural_key=key,
        seat=seat,
        host=host,
        evaluator=evaluator,
        route=route,
        authority_revision="fleet-7",
        quality_ok=True,
        authorized=True,
        review_invariants_ok=True,
    )
    values.update(changes)
    return EligibleWork(**values)


def test_every_handoff_names_complete_bounded_ownership_without_new_approval():
    assert set(ESTATE_HANDOFFS) == {
        "cardstore-to-skrsi",
        "fleet-to-skrsi",
        "mail-to-skrsi",
        "evidence-to-review",
    }
    for handoff in ESTATE_HANDOFFS.values():
        assert handoff.producer and handoff.consumer and handoff.natural_key
        assert handoff.queue_bound > 0 and handoff.timeout_seconds > 0
        assert handoff.retry_attempts >= 0 and handoff.backoff_seconds >= 0
        assert handoff.terminal_evidence and handoff.recovery_owner
        assert handoff.escalation_recipient and handoff.notification_only is True
    review_handoff = ESTATE_HANDOFFS["evidence-to-review"]
    assert review_handoff.natural_key == "source_card+head_revision"
    assert review_handoff.escalation_recipient == "mero"


@pytest.mark.parametrize(
    "snapshot, reason",
    [
        (AuthoritySnapshot("fleet-7", NOW, False), "unavailable"),
        (AuthoritySnapshot("", NOW), "malformed"),
        (AuthoritySnapshot("fleet-7", NOW + timedelta(seconds=1)), "future"),
        (AuthoritySnapshot("fleet-7", NOW - timedelta(minutes=3)), "stale"),
    ],
)
def test_fanout_fails_closed_on_bad_authority_truth(snapshot, reason):
    fanout = BoundedFleetFanout(FanoutBudget(1, 1, 1, 1, 10))
    with pytest.raises(AuthorityUnavailableError, match=reason):
        fanout.allocate([eligible("work-1")], snapshot, now=NOW)


def test_load_fans_out_extra_work_without_duplicates_or_cross_seat_ownership():
    fanout = BoundedFleetFanout(FanoutBudget(2, 3, 3, 4, 8))
    snapshot = AuthoritySnapshot("fleet-7", NOW)
    items = [
        eligible(
            f"work-{i}",
            seat=f"seat-{i % 4}",
            host=f"host-{i % 3}",
            evaluator=f"eval-{i % 3}",
            route=f"route-{i % 4}",
        )
        for i in range(30)
    ]
    # Duplicate keys aimed at other seats model competing workers at saturation.
    items.extend(eligible(f"work-{i}", seat="seat-conflict") for i in range(8))
    receipts = fanout.allocate(items, snapshot, now=NOW)
    assert 1 < len(receipts) <= 8
    assert len({receipt.natural_key for receipt in receipts}) == len(receipts)
    assert not any(receipt.seat == "seat-conflict" for receipt in receipts)
    assert max(sum(r.seat == value for r in receipts) for value in {r.seat for r in receipts}) <= 2
    assert max(sum(r.host == value for r in receipts) for value in {r.host for r in receipts}) <= 3
    assert max(sum(r.evaluator == value for r in receipts) for value in {r.evaluator for r in receipts}) <= 3
    assert max(sum(r.route == value for r in receipts) for value in {r.route for r in receipts}) <= 4


def test_source_head_deduplication_and_lifecycle_scope():
    fanout = BoundedFleetFanout(FanoutBudget(3, 3, 3, 3, 10))
    snapshot = AuthoritySnapshot("fleet-7", NOW)
    first = eligible("natural-a", seat="link", route="integration", source_head="head-1")
    alias = eligible("natural-b", seat="link", route="integration", source_head="head-1")
    receipts = fanout.allocate([first, alias], snapshot, now=NOW)
    assert len(receipts) == 1
    assert receipts[0].natural_key == "natural-a"
    with pytest.raises(SKRSIError, match="scope"):
        fanout.allocate([eligible("bad", seat="tank", route="deploy")], snapshot, now=NOW)


def test_jarvis_is_not_a_recurring_lifecycle_seat():
    assert "jarvis" not in LIFECYCLE_SEATS


def test_saturation_keeps_quality_authorization_review_and_revision_fences():
    fanout = BoundedFleetFanout(FanoutBudget(10, 10, 10, 10, 10))
    snapshot = AuthoritySnapshot("fleet-7", NOW)
    work = [
        eligible("good"),
        eligible("bad-quality", quality_ok=False),
        eligible("unauthorized", authorized=False),
        eligible("bad-review", review_invariants_ok=False),
        eligible("old-revision", authority_revision="fleet-6"),
    ]
    assert [r.natural_key for r in fanout.allocate(work, snapshot, now=NOW)] == ["good"]


def test_concurrent_review_materialization_and_replay_has_one_launch_receipt():
    handoff = ReviewHandoff()

    def launch():
        return handoff.launch(
            source_card="card-1",
            head_revision="a" * 40,
            producer="builder",
            reviewer="seraph",
            process_id="pid-42",
            authority_revision="cards-9",
        )

    with ThreadPoolExecutor(max_workers=24) as pool:
        receipts = list(pool.map(lambda _: launch(), range(100)))
    assert len({r.review_card for r in receipts}) == 1
    assert len({r.claim_id for r in receipts}) == 1
    assert len({r.process_id for r in receipts}) == 1
    assert len({r.receipt_id for r in receipts}) == 1
    assert receipts[0].review_card == canonical_review_card("card-1", "a" * 40)
    assert launch() == receipts[0]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"reviewer": "builder"}, "independent"),
        ({"process_id": "pid-99"}, "immutable"),
    ],
)
def test_review_handoff_rejects_producer_or_second_process_for_same_head(change, message):
    handoff = ReviewHandoff()
    args = dict(
        source_card="card-1",
        head_revision="a" * 40,
        producer="builder",
        reviewer="seraph",
        process_id="pid-42",
        authority_revision="cards-9",
    )
    if change.get("reviewer") != "builder":
        handoff.launch(**args)
    args.update(change)
    with pytest.raises(SKRSIError, match=message):
        handoff.launch(**args)


def test_changed_head_gets_one_distinct_canonical_review_launch():
    handoff = ReviewHandoff()
    common = dict(
        source_card="card-1",
        producer="builder",
        reviewer="seraph",
        authority_revision="cards-9",
    )
    first = handoff.launch(head_revision="a" * 40, process_id="pid-1", **common)
    second = handoff.launch(head_revision="b" * 40, process_id="pid-2", **common)

    assert first.review_card != second.review_card
    assert first.receipt_id != second.receipt_id
    assert first.review_card == canonical_review_card("card-1", "a" * 40)
    assert second.review_card == canonical_review_card("card-1", "b" * 40)
    assert handoff.launch(head_revision="b" * 40, process_id="pid-2", **common) == second


def metric_event(event_id="evt-1", event_type="review.verdict", **changes):
    value = dict(
        event_id=event_id,
        event_type=event_type,
        occurred_at=NOW.isoformat(),
        recorded_at=NOW.isoformat(),
        value=1,
        source="cardstore",
        target_ref="target/software-lifecycle",
        revision="cards-9",
        producer="builder",
        consumer="seraph",
        seat="seat-a",
        host="chiap04",
        model="codex",
        route="review",
        evidence_ref="sha256:" + "a" * 64,
    )
    value.update(changes)
    return value


def test_metric_contract_covers_required_estate_measurements(tmp_path):
    names = {name for name, _ in METRIC_EVENTS.values()}
    assert names >= {
        "skrsi.handoff.latency",
        "skrsi.queue.time",
        "skrsi.reviewer.latency",
        "skrsi.claim.conflicts",
        "skrsi.blockers.repeated",
        "skrsi.throughput",
        "skrsi.review.first_pass_rate",
        "skrsi.rework.count",
        "skrsi.cleanup.yield",
        "skrsi.recovery.success_rate",
        "skrsi.route.attribution",
    }
    outbox = AppendOnlyOutbox(tmp_path / "skrsi.jsonl")
    first = append_metric_event(outbox, metric_event())
    second = append_metric_event(outbox, metric_event())
    assert first == second
    entries = outbox.entries()
    assert len(entries) == 1
    payload = __import__("json").loads(entries[0].serialized)["payload"]
    assert payload["metric"] == "skrsi.review.first_pass_rate"
    assert payload["metadata"] == {
        "producer": "builder",
        "consumer": "seraph",
        "seat": "seat-a",
        "host": "chiap04",
        "model": "codex",
        "route": "review",
        "evidence_ref": "sha256:" + "a" * 64,
    }


def test_structural_state_or_link_cannot_be_inferred_as_verdict(tmp_path):
    outbox = AppendOnlyOutbox(tmp_path / "skrsi.jsonl")
    with pytest.raises(SKRSIError, match="unknown metric event fields"):
        append_metric_event(outbox, metric_event(state="done"))
    with pytest.raises(SKRSIError, match="explicit evidence"):
        append_metric_event(outbox, metric_event(event_type="lifecycle.completed"))
    assert outbox.entries() == ()
