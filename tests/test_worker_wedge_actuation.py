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

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.fleet.worker_watchdog import (
    DEFAULT_PROGRESS_TIMEOUT_S,
    DEFAULT_WEDGE_TIMEOUT_S,
    WEDGE_ACTUATING_STATES,
    ProgressObservation,
    classify_wedge,
    transcript_limit_bytes,
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


def test_actuating_states_are_exactly_the_three_that_were_authorised():
    """Widening this set is a deliberate act, not an accident.

    "wedge-transcript-runaway" was added 2026-09-21 by operator decision, after
    two workers (a81000a2 on chiap03, cf460fde on chiap04) each held a codex
    slot for 9.5 hours while every existing signal reported them healthy: fresh
    heartbeat, fresh transcript mtime, state=progress-fresh. The first had a
    165MB transcript holding 11,423 exploration calls against 4 edits.
    """
    assert WEDGE_ACTUATING_STATES == frozenset(
        {
            "wedge-stale-confirmed",
            "wedge-absent-confirmed",
            "wedge-transcript-runaway",
        }
    )


# --- replay of the real measurement window -----------------------------------
#
# tests/fixtures/worker-progress-chi-20260918.log is the WORKER_PROGRESS lines
# the fleet actually emitted while the pass was report-only: 158 records, 12
# distinct owners, 5 chi hosts, 20260918T193007Z to 20260919T052500Z.
#
# This is the test that answers the only question that matters before turning
# an actuator on: how many genuinely-working workers would it have killed?

FIXTURE = Path(__file__).parent / "fixtures" / "worker-progress-chi-20260918.log"

# The two owners that SHOULD be actuated, and the independent evidence that
# neither was working, gathered outside this signal on 2026-09-19:
#
#   pi-glm-chiap03-139ec63d    the incident. 6h18m held, 0 workspace files,
#                              pi alive at 0.0% CPU, 0-byte stdout log.
#   pi-qwen-chiap01-34115541   unit crashlooping, every relaunch exiting
#                              status=1 in under 2.5min, every worker log
#                              0 bytes, and `scanned=1587` unchanged across
#                              the entire 16-hour stale run, so not one file
#                              was added or touched.
EXPECTED_ACTUATED = {"pi-glm-chiap03-139ec63d", "pi-qwen-chiap01-34115541"}


def _replay():
    """Classify every measured record. Returns owner -> Counter(verdict)."""
    per_owner: dict[str, Counter] = defaultdict(Counter)
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        stamp, _, body = line.partition("\t")
        fields = dict(part.split("=", 1) for part in body.split("|") if "=" in part)
        fields["_at"] = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        fields["_card"] = body.split("|")[3]
        rows.append(fields)
    # The records carry no claim age, so each observation is dated from the
    # owner's FIRST appearance in the window. That is a lower bound on the
    # real hold, which is the conservative direction for the absent case: it
    # can only delay an actuation, never invent one.
    first: dict[tuple[str, str], datetime] = {}
    for row in sorted(rows, key=lambda item: item["_at"]):
        first.setdefault((row["owner"], row["_card"]), row["_at"])
    for row in rows:
        age = row["progress_age_s"]
        progress_at = (
            None if age == "none" else (row["_at"] - timedelta(seconds=int(age))).isoformat()
        )
        observation = ProgressObservation(
            owner=row["owner"],
            card_id=row["_card"],
            session_id="session",
            claim_revision=row["claim_revision"],
            expected_claim_revision=row["claim_revision"],
            progress_at=progress_at,
            session_alive=True,
        )
        per_owner[row["owner"]][
            classify_wedge(
                observation,
                now=row["_at"],
                claim_age_s=(row["_at"] - first[(row["owner"], row["_card"])]).total_seconds(),
                receipt_local=row["receipt"] == "local",
            )
        ] += 1
    return per_owner


def test_the_measured_window_is_the_window_that_was_measured():
    """Guard the fixture itself, so a later edit cannot quietly weaken this."""
    per_owner = _replay()
    assert sum(sum(counts.values()) for counts in per_owner.values()) == 158
    assert len(per_owner) == 12


def test_no_genuinely_working_worker_would_have_been_killed():
    """The whole safety case, replayed against 158 real records.

    Ten of the twelve owners are never actuated even once.  The two that are
    were both independently proven to be producing nothing.
    """
    per_owner = _replay()
    actuated = {
        owner for owner, counts in per_owner.items() if set(counts) & WEDGE_ACTUATING_STATES
    }
    assert actuated == EXPECTED_ACTUATED


def test_every_fresh_observation_refuses():
    """38 of 38 progress-fresh records classify as progressing, none actuate.

    The absent and within-margin counts moved on 2026-09-19 when the deadline
    was re-derived from the agent transcript and lowered 14400 -> 7200: 25
    records crossed from wedge-within-margin to wedge-absent-confirmed,
    because the absent case is deadlined on claim age. Every one of the 25
    belongs to pi-glm-chiap03-139ec63d, the incident worker this reaper was
    built for, which is why the actuated SET in
    test_no_genuinely_working_worker_would_have_been_killed is unchanged.
    The progress-fresh and stale totals are untouched by the new deadline.
    """
    per_owner = _replay()
    total = Counter()
    for counts in per_owner.values():
        total.update(counts)
    assert total["wedge-progressing"] == 38
    assert total["wedge-stale-confirmed"] == 10
    assert total["wedge-absent-confirmed"] == 62
    assert total["wedge-within-margin"] == 48
    # The 25 that moved are all one owner, and it is the incident.
    assert per_owner["pi-glm-chiap03-139ec63d"]["wedge-absent-confirmed"] == 62


def test_the_slow_starting_worker_is_never_touched():
    """pi-glm-chiap03-ea911b09 is the case a naive absent-workspace rule kills.

    It reported progress-missing three times and then went progress-fresh
    nine times under the SAME claim revision: one real worker that took about
    fifteen minutes to populate its workspace before producing anything.  An
    empty workspace is a normal startup state, which is exactly why the
    absent case is deadlined and not acted on the moment it is observed.
    """
    counts = _replay()["pi-glm-chiap03-ea911b09"]
    assert counts["wedge-within-margin"] == 3
    assert counts["wedge-progressing"] == 9
    assert not set(counts) & WEDGE_ACTUATING_STATES


# ── runaway transcript, armed 2026-09-21 ─────────────────────────────────────
#
# Two workers held a codex slot for 9.5 hours while every signal said healthy:
# fresh heartbeat, fresh transcript mtime, state=progress-fresh. A runaway is
# fresh BY DEFINITION because it never stops writing, so size is the evidence
# elapsed time cannot be.


def _runaway_observation(**overrides):
    base = dict(
        owner="pi-codex-testhost-cafe0001",
        card_id="cafe0001",
        session_id="codex-auto-cafe0001",
        claim_revision="rev-1",
        expected_claim_revision="rev-1",
        progress_at=datetime.now(timezone.utc).isoformat(),
        session_alive=True,
    )
    base.update(overrides)
    return ProgressObservation(**base)


def _runaway_wedge(observation, claim_age_s=60.0):
    return classify_wedge(
        observation,
        now=datetime.now(timezone.utc),
        claim_age_s=claim_age_s,
        receipt_local=True,
    )


def test_a_transcript_over_the_limit_is_a_runaway_even_when_fresh():
    """The a81000a2 shape: writing constantly, producing nothing."""
    observation = _runaway_observation(transcript_bytes=165 * 1024 * 1024)
    assert _runaway_wedge(observation) == "wedge-transcript-runaway"


def test_a_runaway_actuates():
    assert "wedge-transcript-runaway" in WEDGE_ACTUATING_STATES


def test_a_transcript_under_the_limit_keeps_the_progress_exemption():
    """A healthy worker measured 2.5MB at 29 minutes; it must not be touched."""
    observation = _runaway_observation(transcript_bytes=2_569_472)
    assert _runaway_wedge(observation) == "wedge-progressing"


def test_exactly_at_the_limit_is_not_a_runaway():
    """Strictly greater-than, so the boundary itself survives."""
    observation = _runaway_observation(transcript_bytes=100 * 1024 * 1024)
    assert _runaway_wedge(observation) == "wedge-progressing"


def test_an_unmeasured_transcript_never_actuates():
    """None is not zero and is not 'huge'. A measurement failure must never
    read as a kill signal, which is the rule the whole module is built on."""
    observation = _runaway_observation(transcript_bytes=None)
    assert _runaway_wedge(observation) == "wedge-progressing"


def test_the_limit_is_overridable_and_a_bad_override_falls_back(monkeypatch):
    monkeypatch.setenv("SKFLEET_TRANSCRIPT_LIMIT_BYTES", "1024")
    assert transcript_limit_bytes() == 1024
    for bad in ("", "   ", "abc", "0", "-5"):
        monkeypatch.setenv("SKFLEET_TRANSCRIPT_LIMIT_BYTES", bad)
        assert transcript_limit_bytes() == 100 * 1024 * 1024


def test_a_lowered_limit_makes_a_small_transcript_a_runaway(monkeypatch):
    """Proves the limit is actually consulted, not a constant folded in."""
    monkeypatch.setenv("SKFLEET_TRANSCRIPT_LIMIT_BYTES", "1000")
    observation = _runaway_observation(transcript_bytes=2000)
    assert _runaway_wedge(observation) == "wedge-transcript-runaway"


def test_a_claim_mismatch_still_beats_the_runaway_check():
    """Identity fences come first: never act on a superseded generation."""
    observation = _runaway_observation(
        transcript_bytes=300 * 1024 * 1024, expected_claim_revision="rev-2"
    )
    assert _runaway_wedge(observation) == "wedge-refused-progress-claim-mismatch"
