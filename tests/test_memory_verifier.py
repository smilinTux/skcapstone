"""Tests for the two-factor memory verification gate (memory_verifier.py).

These tests verify that:
  - Aligned memories are promoted normally.
  - Contradicting memories are tagged=conflicting, a conflict report is stored,
    a critical notification fires, and promotion is skipped.
  - Fail-open behaviour when skseed is unavailable.
  - Short-term → mid-term gate in memory_engine._promote().
  - Short-term → mid-term gate in PromotionEngine._promote().
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.memory_verifier import (
    VerificationResult,
    verify_before_promotion,
)
from skcapstone.models import MemoryEntry, MemoryLayer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    memory_id: str = "abc123",
    content: str = "The sky is blue",
    layer: MemoryLayer = MemoryLayer.SHORT_TERM,
    importance: float = 0.5,
    tags: list[str] | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        memory_id=memory_id,
        content=content,
        tags=tags or [],
        source="test",
        layer=layer,
        importance=importance,
        created_at=datetime.now(timezone.utc),
    )


def _aligned_result(coherence: float = 0.85) -> dict:
    """Simulate a truth_check return that passes alignment."""
    return {
        "is_aligned": True,
        "collider_result": {
            "coherence_score": coherence,
            "truth_grade": "strong",
            "collision_fragments": [],
        },
        "alignment_record": {},
        "belief": {},
    }


def _contradicting_result(
    coherence: float = 0.3,
    fragments: list[str] | None = None,
) -> dict:
    """Simulate a truth_check return that fails alignment."""
    return {
        "is_aligned": False,
        "collider_result": {
            "coherence_score": coherence,
            "truth_grade": "weak",
            "collision_fragments": fragments or ["fragment A contradicts earlier claim"],
        },
        "alignment_record": {},
        "belief": {},
    }


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Minimal agent home directory."""
    mem = tmp_path / "memory"
    for layer in ("short-term", "mid-term", "long-term"):
        (mem / layer).mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# verify_before_promotion — unit tests
# ---------------------------------------------------------------------------


class TestVerifyBeforePromotion:

    def test_fail_open_when_skseed_missing(self, home: Path) -> None:
        """Should allow promotion when skseed cannot be imported."""
        entry = _make_entry()
        with patch.dict(sys.modules, {"skseed": None, "skseed.skill": None}):
            result = verify_before_promotion(home, entry)
        assert result.should_promote is True
        assert result.is_conflicting is False

    def test_fail_open_when_truth_check_raises(self, home: Path) -> None:
        """Should allow promotion when truth_check raises unexpectedly."""
        import skcapstone.memory_verifier as mv

        entry = _make_entry()
        skill_mock = MagicMock()
        skill_mock.truth_check = MagicMock(side_effect=RuntimeError("LLM timeout"))
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            result = mv.verify_before_promotion(home, entry)
        assert result.should_promote is True

    def test_skip_gate_for_mid_term_entries(self, home: Path) -> None:
        """Gate only fires for SHORT_TERM; mid-term entries pass through."""
        entry = _make_entry(layer=MemoryLayer.MID_TERM)
        result = verify_before_promotion(home, entry)
        assert result.should_promote is True
        assert result.is_conflicting is False

    def test_aligned_memory_promotes(self, home: Path) -> None:
        """Aligned truth-check result → should_promote=True, no conflict."""
        entry = _make_entry()
        skill_mock = MagicMock()
        skill_mock.truth_check = MagicMock(return_value=_aligned_result(0.9))
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            result = verify_before_promotion(home, entry)
        assert result.should_promote is True
        assert result.is_conflicting is False
        assert result.coherence_score == pytest.approx(0.9)
        assert result.truth_grade == "strong"

    def test_contradicting_memory_blocked(self, home: Path) -> None:
        """Contradiction found → should_promote=False, is_conflicting=True."""
        entry = _make_entry()
        skill_mock = MagicMock()
        skill_mock.truth_check = MagicMock(return_value=_contradicting_result())
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            with patch("skcapstone.memory_verifier._fire_conflict_notification"):
                result = verify_before_promotion(home, entry)
        assert result.should_promote is False
        assert result.is_conflicting is True
        assert result.collision_fragments != []

    def test_conflicting_tag_added_to_candidate(self, home: Path) -> None:
        """Candidate entry gets tag=conflicting when promotion is blocked."""
        entry = _make_entry(memory_id="cand01")
        # Write the entry to disk so _save_entry works
        path = home / "memory" / "short-term" / "cand01.json"
        path.write_text(entry.model_dump_json(), encoding="utf-8")

        skill_mock = MagicMock()
        skill_mock.truth_check = MagicMock(return_value=_contradicting_result())
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            with patch("skcapstone.memory_verifier._fire_conflict_notification"):
                verify_before_promotion(home, entry)

        # Re-read from disk
        import json
        raw = json.loads(path.read_text())
        assert "conflicting" in raw["tags"]

    def test_conflict_report_stored(self, home: Path) -> None:
        """A conflict-report memory is stored in short-term when blocked."""
        entry = _make_entry(memory_id="cand02", content="contradictory claim")
        path = home / "memory" / "short-term" / "cand02.json"
        path.write_text(entry.model_dump_json(), encoding="utf-8")

        skill_mock = MagicMock()
        skill_mock.truth_check = MagicMock(return_value=_contradicting_result())
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            with patch("skcapstone.memory_verifier._fire_conflict_notification"):
                result = verify_before_promotion(home, entry)

        assert result.conflict_report_id is not None
        # The report file should exist in short-term
        report_path = home / "memory" / "short-term" / f"{result.conflict_report_id}.json"
        assert report_path.exists()
        import json
        report = json.loads(report_path.read_text())
        assert "conflicting" in report["tags"]
        assert "CONFLICT REPORT" in report["content"]
        assert "cand02" in report["content"]

    def test_critical_notification_fired(self, home: Path) -> None:
        """A critical desktop notification fires when promotion is blocked."""
        entry = _make_entry()
        path = home / "memory" / "short-term" / f"{entry.memory_id}.json"
        path.write_text(entry.model_dump_json(), encoding="utf-8")

        skill_mock = MagicMock()
        skill_mock.truth_check = MagicMock(return_value=_contradicting_result())
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            with patch("skcapstone.memory_verifier._fire_conflict_notification") as mock_notif:
                verify_before_promotion(home, entry)
        mock_notif.assert_called_once()

    def test_notification_uses_critical_urgency(self, home: Path) -> None:
        """The actual notify() call receives urgency=critical."""
        entry = _make_entry(memory_id="notif01")
        path = home / "memory" / "short-term" / "notif01.json"
        path.write_text(entry.model_dump_json(), encoding="utf-8")

        skill_mock = MagicMock()
        skill_mock.truth_check = MagicMock(return_value=_contradicting_result())
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            with patch("skcapstone.notifications.notify") as mock_notify:
                import skcapstone.memory_verifier as mv
                # Ensure the module uses our patched notify
                with patch.object(mv, "_fire_conflict_notification",
                                  wraps=mv._fire_conflict_notification):
                    verify_before_promotion(home, entry)
        # At least one call should have urgency=critical
        calls = mock_notify.call_args_list
        assert any(
            call.kwargs.get("urgency") == "critical" or
            (len(call.args) >= 3 and call.args[2] == "critical")
            for call in calls
        ), f"No critical notification call found in {calls}"

    def test_verifier_source_bypasses_gate(self, home: Path) -> None:
        """Conflict-report entries (source=memory_verifier) must not be re-checked."""
        entry = _make_entry(memory_id="meta01")
        entry = _make_entry(memory_id="meta01")
        # Simulate a conflict-report entry
        from dataclasses import replace
        entry2 = MemoryEntry(
            memory_id="meta01",
            content="[CONFLICT REPORT] some conflict",
            tags=["conflicting"],
            source="memory_verifier",
            layer=MemoryLayer.SHORT_TERM,
            importance=0.8,
            created_at=entry.created_at,
        )
        # truth_check should never be called
        skill_mock = MagicMock()
        skill_mock.truth_check = MagicMock(side_effect=AssertionError("should not be called"))
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            result = verify_before_promotion(home, entry2)
        assert result.should_promote is True
        skill_mock.truth_check.assert_not_called()

    def test_low_coherence_no_fragments_also_blocked(self, home: Path) -> None:
        """is_aligned=False with empty fragments still triggers a conflict."""
        entry = _make_entry()
        skill_mock = MagicMock()
        # No fragments, but is_aligned=False
        skill_mock.truth_check = MagicMock(return_value={
            "is_aligned": False,
            "collider_result": {
                "coherence_score": 0.4,
                "truth_grade": "weak",
                "collision_fragments": [],
            },
        })
        with patch.dict(sys.modules, {"skseed": MagicMock(), "skseed.skill": skill_mock}):
            with patch("skcapstone.memory_verifier._fire_conflict_notification"):
                result = verify_before_promotion(home, entry)
        assert result.should_promote is False
        assert result.is_conflicting is True


# ---------------------------------------------------------------------------
# Integration: memory_engine._promote() gate
# ---------------------------------------------------------------------------


class TestMemoryEnginePromoteGate:

    def test_engine_promote_short_term_blocked(self, home: Path) -> None:
        """_promote() in memory_engine stays in short-term when gate says no."""
        from skcapstone import memory_engine

        entry = _make_entry(memory_id="eng01")
        path = home / "memory" / "short-term" / "eng01.json"
        path.write_text(entry.model_dump_json(), encoding="utf-8")

        def _fake_verify(h, e):
            return VerificationResult(
                should_promote=False,
                is_conflicting=True,
                coherence_score=0.3,
                truth_grade="weak",
                collision_fragments=["contradiction"],
            )

        # Patch at the verifier module level (local import picks it up there)
        with patch("skcapstone.memory_verifier.verify_before_promotion", _fake_verify):
            promoted = memory_engine._promote(home, entry, path)

        assert promoted is False
        # Entry should still be in short-term
        assert path.exists()

    def test_engine_promote_short_term_allowed(self, home: Path) -> None:
        """_promote() in memory_engine moves to mid-term when gate passes."""
        from skcapstone import memory_engine

        entry = _make_entry(memory_id="eng02")
        path = home / "memory" / "short-term" / "eng02.json"
        path.write_text(entry.model_dump_json(), encoding="utf-8")

        def _fake_verify(h, e):
            return VerificationResult(should_promote=True, coherence_score=0.9)

        with patch("skcapstone.memory_verifier.verify_before_promotion", _fake_verify):
            promoted = memory_engine._promote(home, entry, path)

        assert promoted is True
        mid_path = home / "memory" / "mid-term" / "eng02.json"
        assert mid_path.exists()

    def test_engine_store_fast_path_blocked(self, home: Path) -> None:
        """store() with importance>=0.7 stays in short-term when gate says no."""
        from skcapstone import memory_engine

        def _fake_verify(h, e):
            return VerificationResult(should_promote=False, is_conflicting=True)

        with patch("skcapstone.memory_verifier.verify_before_promotion", _fake_verify):
            result = memory_engine.store(
                home=home,
                content="Important claim",
                importance=0.8,
            )

        assert result.layer == MemoryLayer.SHORT_TERM

    def test_engine_store_fast_path_allowed(self, home: Path) -> None:
        """store() with importance>=0.7 promotes to mid-term when gate passes."""
        from skcapstone import memory_engine

        def _fake_verify(h, e):
            return VerificationResult(should_promote=True, coherence_score=0.9)

        with patch("skcapstone.memory_verifier.verify_before_promotion", _fake_verify):
            result = memory_engine.store(
                home=home,
                content="Important aligned claim",
                importance=0.8,
            )

        assert result.layer == MemoryLayer.MID_TERM


# ---------------------------------------------------------------------------
# Integration: PromotionEngine._promote() gate
# ---------------------------------------------------------------------------


class TestPromotionEngineGate:

    def test_promoter_sweep_blocked_counts_as_skipped(self, home: Path) -> None:
        """A truth-check-blocked candidate is not in result.promoted."""
        from datetime import timedelta

        from skcapstone.memory_promoter import PromotionEngine, PromotionThresholds

        # Write a short-term entry that scores well above threshold
        from skcapstone.memory_engine import _save_entry

        created = datetime.now(timezone.utc) - timedelta(hours=30)
        entry = MemoryEntry(
            memory_id="sweep01",
            content="decision: use sovereign architecture",
            tags=["architect", "decision", "breakthrough"],
            source="test",
            layer=MemoryLayer.SHORT_TERM,
            importance=0.9,
            access_count=5,
            created_at=created,
        )
        _save_entry(home, entry)

        def _fake_verify(h, e):
            return VerificationResult(should_promote=False, is_conflicting=True)

        engine = PromotionEngine(home, PromotionThresholds(short_to_mid=0.1))
        with patch("skcapstone.memory_verifier.verify_before_promotion", _fake_verify):
            result = engine.sweep(dry_run=False)

        # Candidate was found but not promoted
        assert not any(c.memory_id == "sweep01" and c.promoted for c in result.candidates)

    def test_promoter_sweep_allowed_promotes(self, home: Path) -> None:
        """A truth-check-allowed candidate ends up in mid-term."""
        from datetime import timedelta

        from skcapstone.memory_promoter import PromotionEngine, PromotionThresholds
        from skcapstone.memory_engine import _save_entry

        created = datetime.now(timezone.utc) - timedelta(hours=30)
        entry = MemoryEntry(
            memory_id="sweep02",
            content="decision: sovereign architecture approved",
            tags=["architect", "decision"],
            source="test",
            layer=MemoryLayer.SHORT_TERM,
            importance=0.9,
            access_count=5,
            created_at=created,
        )
        _save_entry(home, entry)

        def _fake_verify(h, e):
            return VerificationResult(should_promote=True, coherence_score=0.95)

        engine = PromotionEngine(home, PromotionThresholds(short_to_mid=0.1))
        # Sweep short-term only so the entry isn't double-promoted in one pass
        with patch("skcapstone.memory_verifier.verify_before_promotion", _fake_verify):
            result = engine.sweep(layer=MemoryLayer.SHORT_TERM, dry_run=False)

        promoted_ids = [c.memory_id for c in result.promoted if c.promoted]
        assert "sweep02" in promoted_ids
        mid_path = home / "memory" / "mid-term" / "sweep02.json"
        assert mid_path.exists()
