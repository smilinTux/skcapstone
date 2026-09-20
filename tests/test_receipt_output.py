"""Regressions for the receipt output condenser.

Measured failure (2026-09-19): one malformed card_events JSONL line made a
CardStore fold emit an identical warning hundreds of times in a single
cycle. Those repeats landed verbatim in a dispatcher receipt's stderr
field, pushed the receipt past journald's 48 KiB per-message cap, and the
receipt arrived truncated at exactly 49152 bytes: unparseable JSON. These
tests pin the fix so that never happens again, regardless of how noisy a
dispatcher subprocess is.
"""

from __future__ import annotations

import json

from skcapstone.receipt_output import (
    MAX_DISPATCHER_TEXT_BYTES,
    condense_dispatcher_output,
)

#: journald's documented per-message cap. The observed truncation in the
#: measured failure happened at exactly this many bytes.
JOURNALD_MESSAGE_CAP_BYTES = 48 * 1024


def test_none_passes_through_unchanged() -> None:
    assert condense_dispatcher_output(None) is None


def test_empty_string_is_untouched() -> None:
    assert condense_dispatcher_output("") == ""


def test_plain_short_text_is_untouched() -> None:
    assert condense_dispatcher_output("POOL|ready=1\n") == "POOL|ready=1\n"


def test_bytes_input_is_decoded() -> None:
    assert condense_dispatcher_output(b"deadline\n") == "deadline\n"


def test_invalid_utf8_bytes_do_not_raise() -> None:
    result = condense_dispatcher_output(b"\xff\xfe not valid utf-8")
    assert isinstance(result, str)


def test_non_string_non_bytes_input_does_not_raise() -> None:
    # Defensive: a caller could pass an odd object. The helper must never
    # raise regardless of what a subprocess capture handed it.
    result = condense_dispatcher_output(12345)
    assert isinstance(result, str)


def test_repeated_identical_line_is_collapsed_with_count() -> None:
    warning = (
        "card_events chiap08.jsonl line 19797 is not a card event, "
        "dropping it from the fold: ValidationError | ..."
    )
    noisy = "\n".join([warning] * 247)

    result = condense_dispatcher_output(noisy)

    assert result == f"{warning}  [repeated 247 times]"
    assert noisy.count(warning) == 247
    assert result.count(warning) == 1


def test_repeated_lines_collapse_around_a_genuine_error() -> None:
    warning = "card_events chiap08.jsonl line 19797 is not a card event"
    real_error = "FATAL: dispatcher could not acquire seat lock"
    noisy = "\n".join([warning] * 200 + [real_error])

    result = condense_dispatcher_output(noisy)

    assert "[repeated 200 times]" in result
    assert real_error in result
    assert result.count(warning) == 1


def test_single_occurrence_is_not_marked_as_repeated() -> None:
    result = condense_dispatcher_output("one line only")
    assert result == "one line only"
    assert "repeated" not in result


def test_near_identical_lines_differing_only_in_trailing_whitespace_collapse() -> None:
    lines = ["same warning", "same warning ", "same warning\t", "same warning"]
    result = condense_dispatcher_output("\n".join(lines))
    assert "[repeated 4 times]" in result


def test_bounded_size_keeps_head_and_tail_and_marks_elision() -> None:
    head = "START-OF-RUN marker\n"
    tail = "TAIL-FAILURE: this is the actual error\n"
    # Unique filler so nothing collapses; forces genuine truncation.
    filler = "\n".join(f"unique noise line {i}" for i in range(20_000))
    text = head + filler + "\n" + tail

    result = condense_dispatcher_output(text)

    assert len(result.encode("utf-8")) <= MAX_DISPATCHER_TEXT_BYTES
    assert "START-OF-RUN marker" in result
    assert "TAIL-FAILURE: this is the actual error" in result
    assert "bytes elided" in result


def test_realistic_200kb_noisy_stderr_yields_valid_bounded_receipt() -> None:
    warning = (
        "card_events chiap08.jsonl line 19797 is not a card event, "
        "dropping it from the fold: ValidationError | field required"
    )
    real_failure = "TRACEBACK: dispatcher exited with code 1 after seat lock timeout"
    # Build roughly 200KB of noise: thousands of the identical fold
    # warning, with the real failure at the very end where a truncating
    # cap would have destroyed it.
    repeat_count = 3000
    noisy_stderr = "\n".join([warning] * repeat_count + [real_failure])
    assert len(noisy_stderr.encode("utf-8")) > 200_000

    condensed_stdout = condense_dispatcher_output("")
    condensed_stderr = condense_dispatcher_output(noisy_stderr)

    receipt = {
        "at": "2026-09-19T00:00:00+00:00",
        "seat": "niobe",
        "host": "chiap08",
        "cycle_id": "a" * 32,
        "result": "dispatch_failed",
        "dispatcher_returncode": 1,
        "dispatcher_stdout": condensed_stdout,
        "dispatcher_stderr": condensed_stderr,
    }
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")

    # Must round-trip through JSON, and must fit inside journald's cap
    # with real room to spare for the rest of the receipt.
    decoded = json.loads(encoded)
    assert decoded == receipt
    assert len(encoded) < JOURNALD_MESSAGE_CAP_BYTES
    assert real_failure in decoded["dispatcher_stderr"]
    assert "repeated" in decoded["dispatcher_stderr"]


def test_condensing_is_deterministic() -> None:
    text = "\n".join(["repeat me"] * 50 + ["and a unique tail line"])
    first = condense_dispatcher_output(text)
    second = condense_dispatcher_output(text)
    assert first == second


def test_custom_max_bytes_is_respected() -> None:
    text = "\n".join(f"unique line {i}" for i in range(500))
    result = condense_dispatcher_output(text, max_bytes=512)
    assert len(result.encode("utf-8")) <= 512
