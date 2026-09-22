"""One generation-aware truth governs independent-review eligibility."""

from __future__ import annotations

import pytest

from skcapstone.review_admission import review_generation_eligibility


def _core() -> dict[str, object]:
    """Return one fully bound governed review candidate."""
    return {
        "id": "deadbeef",
        "title": "[REVIEW][S] exact candidate",
        "meta": {
            "producer_identity": "producer",
            "candidate_evidence_sha256": "a" * 64,
            "link_source_card": "source01",
            "link_head_revision": "b" * 40,
        },
    }


def _event(sequence: int, action: str, **fields: object) -> dict[str, object]:
    """Build one deterministically ordered CardStore event."""
    return {
        "ts": f"2026-09-22T15:00:{sequence:02d}+00:00",
        "writer": "reviewer",
        "seq": sequence,
        "action": action,
        **fields,
    }


def test_active_current_generation_is_review_eligible() -> None:
    result = review_generation_eligibility(_core(), ["review", "seat-seraph"], [])

    assert result.applicable is True
    assert result.eligible is True
    assert result.reason is None


@pytest.mark.parametrize(
    "verdict",
    [
        "PASS",
        "FAIL_FOR_REPAIR",
        "FAIL_CLOSED",
        "FAIL_ROLLED_BACK",
        "BLOCKED blocked_on=capability referent=ac:1",
    ],
)
def test_terminal_verdict_withholds_exact_review_generation(verdict: str) -> None:
    result = review_generation_eligibility(
        _core(),
        ["review", "seat-seraph"],
        [_event(1, "link", link_key="verdict", link_value=verdict)],
    )

    assert result.eligible is False
    assert result.reason == "terminal-review-generation"


@pytest.mark.parametrize(
    ("kind", "itil_status"),
    [("incident", "detected"), ("problem", "known_error")],
)
def test_explicit_itil_dispatch_hold_is_ineligible(kind: str, itil_status: str) -> None:
    """Stale ITIL projections cannot enter worker or Seraph dispatch."""
    result = review_generation_eligibility(
        {"kind": kind, "meta": {"itil_status": itil_status}},
        [],
        [],
    )

    assert result.applicable is True
    assert result.eligible is False
    assert result.reason == "itil-dispatch-hold"


@pytest.mark.parametrize(
    ("kind", "itil_status"),
    [("incident", "investigating"), ("problem", "analyzing")],
)
def test_other_itil_states_are_outside_explicit_dispatch_hold(kind: str, itil_status: str) -> None:
    """The ITIL exception stays limited to audited parked projections."""
    result = review_generation_eligibility(
        {"kind": kind, "meta": {"itil_status": itil_status}}, [], []
    )

    assert result.applicable is False
    assert result.eligible is True


def test_completed_then_reopened_same_generation_stays_ineligible() -> None:
    result = review_generation_eligibility(
        _core(),
        ["review", "seat-seraph"],
        [
            _event(1, "complete"),
            _event(2, "reopen", column="review"),
            _event(
                3,
                "link",
                link_key="candidate_evidence_sha256",
                link_value="a" * 64,
            ),
        ],
    )

    assert result.eligible is False
    assert result.reason == "terminal-review-generation"


def test_newer_exact_candidate_generation_reopens_review() -> None:
    result = review_generation_eligibility(
        _core(),
        ["review", "seat-seraph"],
        [
            _event(1, "link", link_key="verdict", link_value="PASS"),
            _event(
                2,
                "link",
                link_key="candidate_evidence_sha256",
                link_value="c" * 64,
            ),
            _event(
                3,
                "link",
                link_key="link_head_revision",
                link_value="d" * 40,
            ),
            _event(4, "reopen", column="review"),
        ],
    )

    assert result.eligible is True
    assert result.reason is None


def test_ordinary_task_is_outside_review_generation_gate() -> None:
    result = review_generation_eligibility(
        {"id": "deadbeef", "title": "[S] ordinary task"},
        ["source-only"],
        [_event(1, "link", link_key="verdict", link_value="PASS")],
    )

    assert result.applicable is False
    assert result.eligible is True
