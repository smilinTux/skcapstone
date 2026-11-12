import json

import pytest

from skcapstone.skrsi_pi_adapter import (
    PiAdapterError,
    collect_turns,
    parse_turns,
    record_lane_verdict,
)
from skcapstone.skrsi_registry import AppendOnlyOutbox

TARGET_REF = "target/pi-lane"
NOW = "2026-09-08T20:00:00.000Z"


def stream(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def session_header() -> str:
    return (
        '{"type":"session","version":3,"id":"sess-1",'
        '"timestamp":"2026-09-08T19:00:00.000Z","cwd":"/x"}'
    )


def turn_end(
    input_tokens: int = 1986,
    output: int = 2,
    cache_read: int = 768,
    cache_write: int = 0,
) -> str:
    return json.dumps(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "timestamp": 1757350800000,
                "usage": {
                    "input": input_tokens,
                    "output": output,
                    "cacheRead": cache_read,
                    "cacheWrite": cache_write,
                    "totalTokens": 2756,
                },
            },
        }
    )


def records(outbox: AppendOnlyOutbox) -> list[dict]:
    return [json.loads(entry.serialized) for entry in outbox.entries()]


def test_final_usage_is_taken_from_turn_end_not_message_update_sum():
    lines = [
        session_header(),
        '{"type":"turn_start"}',
        '{"type":"message_update","usage":{"input":100,"output":1,"cacheRead":10,'
        '"cacheWrite":0,"totalTokens":101}}',
        '{"type":"message_update","usage":{"input":200,"output":2,"cacheRead":20,'
        '"cacheWrite":0,"totalTokens":202}}',
        turn_end(input_tokens=1986, output=2, cache_read=768, cache_write=0),
    ]
    turns, rejected = parse_turns(stream(*lines))
    assert len(turns) == 1
    assert len(rejected) == 0
    assert turns[0].usage.input == 1986  # the final value, not 300
    assert turns[0].session_id == "sess-1"
    assert turns[0].index == 1


def test_collect_turns_emits_four_metrics_per_turn():
    turns, _ = parse_turns(stream(session_header(), turn_end()))
    outbox = AppendOnlyOutbox()
    count = collect_turns(outbox, turns, target_ref=TARGET_REF)
    assert count == 4
    metrics = {
        record["payload"]["metric"]: record["payload"]["value"] for record in records(outbox)
    }
    assert metrics == {
        "skrsi.pi.turn.input_tokens": 1986,
        "skrsi.pi.turn.output_tokens": 2,
        "skrsi.pi.turn.cache_read_tokens": 768,
        "skrsi.pi.turn.cache_write_tokens": 0,
    }


def test_verdict_fold_vocabulary_maps_pass_fail_blocked():
    outbox = AppendOnlyOutbox()
    for i, verdict in enumerate(("PASS", "FAIL", "BLOCKED")):
        record_lane_verdict(
            outbox,
            lane_id="lane-1",
            evidence_sha256=str(i) * 64,
            folded_verdict=verdict,
            review_rounds=2,
            occurred_at=NOW,
            recorded_at=NOW,
            target_ref=TARGET_REF,
        )
    values = [
        record["payload"]["value"]
        for record in records(outbox)
        if record["event_type"] == "lane.report.verdict"
    ]
    assert values == [1, 0, 0]


def test_verdict_rejects_accept_literal():
    outbox = AppendOnlyOutbox()
    with pytest.raises(PiAdapterError):
        record_lane_verdict(
            outbox,
            lane_id="lane-1",
            evidence_sha256="a" * 64,
            folded_verdict="ACCEPT",
            review_rounds=2,
            occurred_at=NOW,
            recorded_at=NOW,
            target_ref=TARGET_REF,
        )


def test_verdict_source_is_review_authority():
    outbox = AppendOnlyOutbox()
    record_lane_verdict(
        outbox,
        lane_id="lane-1",
        evidence_sha256="a" * 64,
        folded_verdict="PASS",
        review_rounds=3,
        occurred_at=NOW,
        recorded_at=NOW,
        target_ref=TARGET_REF,
    )
    verdict = next(
        record for record in records(outbox) if record["event_type"] == "lane.report.verdict"
    )
    assert verdict["payload"]["source"] == "seraph"
    rounds = next(
        record for record in records(outbox) if record["event_type"] == "lane.report.rounds"
    )
    assert rounds["payload"]["value"] == 3


def test_negative_usage_fails_closed():
    turns, rejected = parse_turns(stream(session_header(), turn_end(input_tokens=-5)))
    assert len(turns) == 0
    assert len(rejected) == 1


def test_truncated_line_is_rejected():
    raw = session_header() + "\n" + '{"type":"turn_end","message":{"role":"assistant"'
    turns, rejected = parse_turns(raw.encode("utf-8"))
    assert len(turns) == 0
    assert len(rejected) == 1
