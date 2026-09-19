"""The wedge fence: when a live-but-silent worker may lose its claim.

Context, measured on the chi estate 2026-09-19.  A worker held card
``139ec63d`` for 6h18m having written nothing.  Every liveness signal the
fleet had said it was healthy:

* the wrapper beat said ``disposition=RUNNING`` with a 39s age for the whole
  6h18m, because the beat is a ``while :; do ... sleep`` loop in the worker's
  bash shell and is not coupled to ``pi`` at all;
* the systemd unit was active, so ``publish_live`` listed the card as running
  and ``reap_dead_claims`` skipped it by design;
* ``pi`` itself was alive at 0.0% CPU in state ``Sl``.

Only the workspace mtime separated it from the genuinely-working long
runners.  ``classify_progress`` already measured that and was wired
report-only on 2026-09-18 pending a measurement day.  These tests are the
fence that turns the measurement into an action, and every one of them is a
statement about when the fence must REFUSE.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.fleet.worker_watchdog import (
    DEFAULT_PROGRESS_TIMEOUT_S,
    DEFAULT_WEDGE_TIMEOUT_S,
    WEDGE_ACTUATING_STATES,
    ProgressObservation,
    classify_wedge,
    wedge_actuation_fenced,
)

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
OWNER = "pi-glm-chiap03-139ec63d"
REVISION = "revision-1"


def _ago(seconds: float) -> str:
    return (NOW - timedelta(seconds=seconds)).isoformat()


def _obs(**changes: object) -> ProgressObservation:
    values: dict[str, object] = {
        "owner": OWNER,
        "card_id": "139ec63d",
        "session_id": "glm-auto-139ec63d",
        "claim_revision": REVISION,
        "expected_claim_revision": REVISION,
        "progress_at": _ago(6 * 3600 + 18 * 60),
    }
    values.update(changes)
    return ProgressObservation(**values)  # type: ignore[arg-type]


def _wedge(**kwargs: object) -> str:
    call: dict[str, object] = {
        "receipt_local": True,
        "claim_age_s": 6 * 3600 + 18 * 60,
        "now": NOW,
    }
    observation = call.pop("observation", None) or _obs()
    call.update(kwargs)
    observation = call.pop("observation", observation)
    return classify_wedge(observation, **call)  # type: ignore[arg-type]


# --- the margin: the whole point of the fence -------------------------------


def test_wedge_timeout_is_far_above_the_report_only_progress_timeout():
    """900s classifies; it must never be what KILLS.

    ``progress-stale`` starts at ``DEFAULT_PROGRESS_TIMEOUT_S`` (900s) and the
    measured fleet is full of genuinely-working workers past it.  The
    actuating threshold is a different, much larger number on purpose.
    """
    assert DEFAULT_WEDGE_TIMEOUT_S >= 8 * DEFAULT_PROGRESS_TIMEOUT_S


def test_the_incident_worker_is_actuated():
    """6h18m of silence with a local receipt is the case this exists for."""
    assert _wedge() == "wedge-stale-confirmed"


@pytest.mark.parametrize(
    "age_s",
    [
        DEFAULT_PROGRESS_TIMEOUT_S + 1,  # stale, but only just
        DEFAULT_WEDGE_TIMEOUT_S - 1,  # one second short of the deadline
        DEFAULT_WEDGE_TIMEOUT_S,  # exactly at it: strictly-greater required
    ],
)
def test_stale_inside_the_margin_never_actuates(age_s: float):
    """A worker between "stale" and "wedged" is left completely alone."""
    state = _wedge(observation=_obs(progress_at=_ago(age_s)), claim_age_s=age_s)
    assert state == "wedge-within-margin"
    assert state not in WEDGE_ACTUATING_STATES


def test_a_recent_write_is_never_wedged_however_long_the_worker_has_run():
    """Long is not the same as wedged.

    ``abe011e9`` ran 4.2 hours and wrote 2,351 files in its last hour while
    emitting nothing the board could see.  Elapsed time is not evidence.
    """
    state = _wedge(
        observation=_obs(progress_at=_ago(60)),
        claim_age_s=30 * 24 * 3600,
    )
    assert state == "wedge-progressing"
    assert state not in WEDGE_ACTUATING_STATES


# --- the two cases are NOT the same ------------------------------------------


def test_absent_workspace_with_a_local_receipt_is_actuated():
    """No workspace at all, at a path the admission receipt names, is wedged.

    The receipt records the exact path this generation was launched with, so
    its absence is positive proof about THIS worker.
    """
    state = _wedge(observation=_obs(progress_at=None), receipt_local=True)
    assert state == "wedge-absent-confirmed"


def test_absent_workspace_without_a_local_receipt_is_unmeasured_not_wedged():
    """An inferred path that does not exist proves nothing about the worker.

    Without a host-local receipt the reporter GUESSES the workspace from the
    owner name.  A miss there is a measurement failure, and a measurement
    failure must never read as a kill signal.
    """
    state = _wedge(observation=_obs(progress_at=None), receipt_local=False)
    assert state == "wedge-unmeasured"
    assert state not in WEDGE_ACTUATING_STATES


def test_absent_workspace_inside_the_margin_never_actuates():
    """A worker that has not been alive long enough to have written yet."""
    state = _wedge(observation=_obs(progress_at=None), claim_age_s=120)
    assert state == "wedge-within-margin"


def test_stale_workspace_does_not_need_a_local_receipt():
    """A real mtime is self-evidencing wherever the path came from.

    Absence is only meaningful against an authoritative path; a WRITE that is
    six hours old is the same fact no matter how the directory was located.
    """
    assert _wedge(receipt_local=False) == "wedge-stale-confirmed"


# --- fail-closed on every non-actionable progress state ----------------------


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"claim_revision": "other"}, "wedge-refused-progress-claim-mismatch"),
        ({"owner": ""}, "wedge-refused-progress-invalid-identity"),
        ({"progress_at": "not-a-time"}, "wedge-refused-progress-malformed"),
        ({"terminal_evidence_seen": True}, "wedge-refused-progress-terminal-evidence"),
        (
            {"progress_at": _ago(-600)},
            "wedge-refused-progress-clock-skew",
        ),
        (
            {"process_alive": False, "session_alive": False},
            "wedge-refused-progress-exited",
        ),
    ],
)
def test_every_other_progress_state_refuses(changes: dict[str, object], expected: str):
    """Only stale and absent actuate.  Everything else is someone else's path.

    ``progress-exited`` in particular belongs to the absence reaper, which has
    a quorum gate this path deliberately does not have.
    """
    state = _wedge(observation=_obs(**changes))
    assert state == expected
    assert state not in WEDGE_ACTUATING_STATES


def test_unknown_claim_age_refuses():
    """No age, no deadline, no action."""
    assert _wedge(claim_age_s=None) == "wedge-unmeasured"


def test_negative_claim_age_refuses():
    assert _wedge(claim_age_s=-1) == "wedge-unmeasured"


# --- the exact-generation fence ---------------------------------------------


def test_fence_requires_the_exact_owner_and_revision():
    """A newer generation must never be released by an older observation."""
    observation = _obs()
    assert (
        wedge_actuation_fenced(
            observation,
            owner=OWNER,
            claim_revision=REVISION,
            now=NOW,
            receipt_local=True,
            claim_age_s=6 * 3600,
        )
        is True
    )
    for wrong in ({"owner": "pi-glm-chiap03-other"}, {"claim_revision": "revision-2"}):
        call = {
            "owner": OWNER,
            "claim_revision": REVISION,
            "now": NOW,
            "receipt_local": True,
            "claim_age_s": 6 * 3600,
        }
        call.update(wrong)
        assert (
            wedge_actuation_fenced(observation, **call) is False  # type: ignore[arg-type]
        ), wrong


def test_actuating_states_are_exactly_two():
    """Widening this set is a deliberate act, not an accident."""
    assert WEDGE_ACTUATING_STATES == frozenset({"wedge-stale-confirmed", "wedge-absent-confirmed"})
