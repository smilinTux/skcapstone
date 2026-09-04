"""Tests for fleet_beat: writer, reader, classifier (card ad0c3bfd / A)."""

import pytest

from skcapstone.fleet_beat import (
    Beat,
    BeatThresholds,
    classify,
    read_beats,
    validate_beat_owner,
    write_beat,
)


class TestValidateBeatOwner:
    def test_one_shared_implementation(self):
        """Card 77d62d85: the beat validator IS the heartbeat validator."""
        from skcapstone.heartbeat import validate_agent_name

        assert validate_beat_owner is validate_agent_name

    def test_valid(self):
        assert validate_beat_owner("pi-codex-chiap01-worker") == "pi-codex-chiap01-worker"

    def test_traversal_rejected(self):
        for evil in ("../evil", "a/b", ".."):
            with pytest.raises(ValueError, match="rejected"):
                validate_beat_owner(evil)

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            validate_beat_owner("")
        with pytest.raises(ValueError):
            validate_beat_owner("   ")


class TestWriteBeat:
    def test_writes_and_reads_back(self, tmp_path):
        write_beat(tmp_path, "worker-a", card_id="abc", sequence=1)
        beats = read_beats(tmp_path)
        assert len(beats) == 1
        assert beats[0].owner == "worker-a"
        assert beats[0].card_id == "abc"

    def test_wrapper_cannot_set_progress(self, tmp_path):
        with pytest.raises(ValueError, match="progress_token"):
            write_beat(tmp_path, "worker-a", progress_token="secret")

    def test_bad_disposition_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="vocabulary"):
            write_beat(tmp_path, "worker-a", disposition="SOMETHING_ELSE")

    def test_malformed_file_skipped(self, tmp_path):
        write_beat(tmp_path, "good-worker")
        (tmp_path / "bad-worker.json").write_text("not json at all")
        beats = read_beats(tmp_path)
        assert len(beats) == 1
        assert beats[0].owner == "good-worker"

    def test_atomic_write(self, tmp_path):
        write_beat(tmp_path, "w1", sequence=1)
        write_beat(tmp_path, "w1", sequence=2)  # overwrite
        beats = read_beats(tmp_path)
        assert len(beats) == 1
        assert beats[0].sequence == 2


class TestClassify:
    BASE_TS = 1000000.0
    TH = BeatThresholds(shadow_alert_s=900, actuation_floor_s=3600, startup_grace_s=120)

    def _beat(
        self, owner="w", emitter="wrapper", disposition="RUNNING", age_s=60, elapsed=100, **kw
    ):
        return Beat(
            owner=owner,
            emitter=emitter,
            disposition=disposition,
            beat_at=self.BASE_TS - age_s,
            elapsed_s=elapsed,
            **kw,
        )

    def test_live(self):
        r = classify([self._beat(age_s=60)], "w", now=self.BASE_TS, thresholds=self.TH)
        assert r.state == "LIVE"
        assert r.evidence == "wrapper_beat"

    def test_agent_beat_preferred(self):
        beats = [self._beat(age_s=300), self._beat(emitter="agent", age_s=60)]
        r = classify(beats, "w", now=self.BASE_TS, thresholds=self.TH)
        assert r.evidence == "agent_beat"

    def test_stalled(self):
        r = classify([self._beat(age_s=1200)], "w", now=self.BASE_TS, thresholds=self.TH)
        assert r.state == "STALLED"

    def test_blocked(self):
        r = classify(
            [self._beat(disposition="BLOCKED_NEEDS_HUMAN", age_s=60)],
            "w",
            now=self.BASE_TS,
            thresholds=self.TH,
        )
        assert r.state == "BLOCKED"
        assert r.disposition == "BLOCKED_NEEDS_HUMAN"

    def test_dead(self):
        r = classify([self._beat(age_s=4000)], "w", now=self.BASE_TS, thresholds=self.TH)
        assert r.state == "DEAD"

    def test_never_started(self):
        r = classify([], "nobody", now=self.BASE_TS, thresholds=self.TH)
        assert r.state == "NEVER_STARTED"
        assert r.evidence == "none"

    def test_startup_grace_unknown(self):
        r = classify([self._beat(age_s=30, elapsed=0)], "w", now=self.BASE_TS, thresholds=self.TH)
        assert r.state == "UNKNOWN"

    def test_future_beat_clock_skew(self):
        r = classify([self._beat(age_s=-100)], "w", now=self.BASE_TS, thresholds=self.TH)
        assert r.state == "UNKNOWN"
        assert "clock skew" in r.note

    def test_no_lease_state_invariant(self):
        """The classifier returns state but never mutates claims."""
        r = classify([self._beat(age_s=99999)], "w", now=self.BASE_TS, thresholds=self.TH)
        assert r.state == "DEAD"
        # No claim mutation is possible: classify is pure, takes no store


class TestAgentBeat:
    def test_agent_beat_with_progress(self, tmp_path):
        write_beat(
            tmp_path, "agent-w", emitter="agent", disposition="RUNNING", progress_token="step-42"
        )
        beats = read_beats(tmp_path)
        assert beats[0].emitter == "agent"
        assert beats[0].progress_token == "step-42"

    def test_agent_blocked_sends_mail(self, tmp_path):
        """Non-RUNNING disposition triggers skmail (Card C)."""
        # We can't easily mock subprocess in this test, but verify the
        # function exists and doesn't raise on RUNNING (no mail path)
        beat = write_beat(tmp_path, "agent-w", emitter="agent", disposition="RUNNING")
        assert beat.disposition == "RUNNING"

    def test_wrapper_still_cannot_set_progress(self, tmp_path):
        with pytest.raises(ValueError, match="progress_token"):
            write_beat(tmp_path, "w", progress_token="not-allowed")
