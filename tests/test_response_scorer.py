"""Tests for skcapstone.response_scorer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.response_scorer import (
    ResponseScore,
    _score_coherence,
    _score_latency,
    _score_length,
    score_response,
)


# ---------------------------------------------------------------------------
# ResponseScore dataclass
# ---------------------------------------------------------------------------


class TestResponseScore:
    """Tests for ResponseScore construction and overall computation."""

    def test_overall_is_weighted_average(self) -> None:
        """overall = coherence*0.4 + length*0.3 + latency*0.3."""
        s = ResponseScore(length_score=1.0, coherence_score=1.0, latency_score=1.0)
        assert s.overall == 1.0

    def test_overall_zero_all_zeros(self) -> None:
        """All-zero dimensions → overall 0.0."""
        s = ResponseScore(length_score=0.0, coherence_score=0.0, latency_score=0.0)
        assert s.overall == 0.0

    def test_overall_mixed(self) -> None:
        """Verify weighted formula with asymmetric scores."""
        s = ResponseScore(length_score=0.0, coherence_score=1.0, latency_score=0.0)
        # overall = 1.0*0.4 + 0.0*0.3 + 0.0*0.3 = 0.4
        assert abs(s.overall - 0.4) < 1e-4

    def test_to_dict_has_all_keys(self) -> None:
        """to_dict contains length, coherence, latency, overall."""
        s = ResponseScore(length_score=0.8, coherence_score=0.7, latency_score=0.9)
        d = s.to_dict()
        assert set(d.keys()) == {"length", "coherence", "latency", "overall"}

    def test_to_dict_values_match(self) -> None:
        """to_dict values match the dataclass attributes."""
        s = ResponseScore(length_score=0.5, coherence_score=0.6, latency_score=0.7)
        d = s.to_dict()
        assert d["length"] == s.length_score
        assert d["coherence"] == s.coherence_score
        assert d["latency"] == s.latency_score
        assert d["overall"] == s.overall


# ---------------------------------------------------------------------------
# Length scoring
# ---------------------------------------------------------------------------


class TestScoreLength:
    """Tests for _score_length."""

    def test_empty_response_is_zero(self) -> None:
        """Empty response → 0.0."""
        assert _score_length("Tell me about Python", "") == 0.0

    def test_response_in_ideal_range_is_one(self) -> None:
        """Response in [lo, hi] word range → 1.0."""
        question = "What is Python?"
        # question has 3 words → lo=max(10,3)=10, hi=max(50,30)=50
        response = " ".join(["word"] * 20)
        assert _score_length(question, response) == 1.0

    def test_very_short_response_penalised(self) -> None:
        """One-word response for a normal question → score < 0.5."""
        question = "Explain the history of artificial intelligence in detail."
        score = _score_length(question, "ok")
        assert score < 0.5

    def test_very_long_response_penalised_gently(self) -> None:
        """Extremely long response → score 0.3 (verbose floor)."""
        question = "Hi"
        response = " ".join(["word"] * 5000)
        score = _score_length(question, response)
        assert score <= 0.5

    def test_long_response_floor_is_0_3(self) -> None:
        """Length score never drops below 0.3 even for very long responses."""
        score = _score_length("hello", " ".join(["x"] * 10_000))
        assert score >= 0.3

    def test_single_word_question(self) -> None:
        """Single-word question — lo=10; response of 15 words → 1.0."""
        score = _score_length("Hi", " ".join(["word"] * 15))
        assert score == 1.0


# ---------------------------------------------------------------------------
# Coherence scoring
# ---------------------------------------------------------------------------


class TestScoreCoherence:
    """Tests for _score_coherence."""

    def test_empty_response_is_zero(self) -> None:
        """Empty response → 0.0."""
        assert _score_coherence("What is Python?", "") == 0.0

    def test_whitespace_response_is_zero(self) -> None:
        """Whitespace-only response → 0.0."""
        assert _score_coherence("What is Python?", "   ") == 0.0

    def test_all_keywords_present_is_one(self) -> None:
        """Response contains all question content words → 1.0."""
        question = "What is Python?"
        # 'python' and 'what'? 'what' is a stopword, 'is' is a stopword.
        # Only 'python' is a content word → 1 keyword
        response = "Python is a high-level programming language."
        assert _score_coherence(question, response) == 1.0

    def test_no_keywords_present_is_zero(self) -> None:
        """Response unrelated to question → low coherence."""
        question = "Explain neural networks"
        response = "The weather today is sunny and warm in Paris."
        score = _score_coherence(question, response)
        assert score < 0.5

    def test_partial_overlap(self) -> None:
        """Partial keyword overlap → intermediate score."""
        question = "Compare Python and JavaScript performance"
        # Content words: compare, python, javascript, performance
        response = "Python is great for scripting."
        # 'python' matches — 1/4 = 0.25
        score = _score_coherence(question, response)
        assert 0.0 < score < 1.0

    def test_question_all_stopwords_returns_neutral(self) -> None:
        """Question with only stopwords → neutral 0.5."""
        assert _score_coherence("is it the", "yes it is") == 0.5

    def test_case_insensitive(self) -> None:
        """Matching is case-insensitive."""
        score = _score_coherence("What is PYTHON?", "python is a language")
        assert score == 1.0


# ---------------------------------------------------------------------------
# Latency scoring
# ---------------------------------------------------------------------------


class TestScoreLatency:
    """Tests for _score_latency."""

    def test_zero_latency_is_one(self) -> None:
        """Zero (or negative) latency → 1.0."""
        assert _score_latency(0.0) == 1.0
        assert _score_latency(-100.0) == 1.0

    def test_under_500ms_is_one(self) -> None:
        """Under 500 ms → 1.0."""
        assert _score_latency(499.9) == 1.0
        assert _score_latency(500.0) == 1.0

    def test_1000ms_is_0_9(self) -> None:
        """1000 ms → 0.9."""
        assert _score_latency(1000.0) == 0.9

    def test_3000ms_is_0_7(self) -> None:
        """3000 ms → 0.7."""
        assert _score_latency(3000.0) == 0.7

    def test_10000ms_is_0_4(self) -> None:
        """10 s → 0.4."""
        assert _score_latency(10_000.0) == 0.4

    def test_20000ms_is_0_2(self) -> None:
        """20 s → 0.2."""
        assert _score_latency(20_000.0) == 0.2

    def test_over_30s_is_0_1(self) -> None:
        """Over 30 s → 0.1 (floor)."""
        assert _score_latency(60_000.0) == 0.1
        assert _score_latency(30_001.0) == 0.1


# ---------------------------------------------------------------------------
# score_response (integration)
# ---------------------------------------------------------------------------


class TestScoreResponse:
    """Tests for the public score_response function."""

    def test_returns_response_score(self) -> None:
        """score_response returns a ResponseScore instance."""
        result = score_response("What is Python?", "Python is a language.", 300.0)
        assert isinstance(result, ResponseScore)

    def test_overall_in_range(self) -> None:
        """overall is always in [0, 1]."""
        result = score_response("", "", 99999.0)
        assert 0.0 <= result.overall <= 1.0

    def test_good_response_high_score(self) -> None:
        """Coherent, appropriately-length, fast response scores >= 0.6."""
        question = "Explain what Python programming language is used for"
        response = (
            "Python is a versatile programming language used for web development, "
            "data science, automation, and artificial intelligence. It is known for "
            "its readable syntax and large ecosystem of libraries."
        )
        result = score_response(question, response, 800.0)
        assert result.overall >= 0.6

    def test_empty_response_low_score(self) -> None:
        """Empty response → low overall score."""
        result = score_response("Explain the universe", "", 200.0)
        assert result.overall < 0.4

    def test_slow_response_penalises_latency(self) -> None:
        """Latency score is low for slow responses."""
        result = score_response("Hi", "Hello!", 60_000.0)
        assert result.latency_score == 0.1

    def test_to_dict_is_json_serializable(self) -> None:
        """score_response().to_dict() can be serialized to JSON."""
        result = score_response("test question", "test answer", 500.0)
        serialized = json.dumps(result.to_dict())
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# ConsciousnessMetrics integration
# ---------------------------------------------------------------------------


class TestConsciousnessMetricsQuality:
    """Tests for quality scoring integration in ConsciousnessMetrics."""

    @pytest.fixture
    def cm(self, tmp_path: Path):
        """ConsciousnessMetrics with no background thread."""
        from skcapstone.metrics import ConsciousnessMetrics
        return ConsciousnessMetrics(home=tmp_path, persist_interval=0)

    def test_initial_quality_avg_zeros(self, cm) -> None:
        """quality_avg returns zeros when nothing recorded."""
        q = cm.quality_avg()
        assert q["count"] == 0
        assert q["overall"] == 0.0

    def test_record_quality_increments_count(self, cm) -> None:
        """record_quality increments the count."""
        score = score_response("What is 2+2?", "2+2 equals 4.", 100.0)
        cm.record_quality(score)
        cm.record_quality(score)
        assert cm.quality_avg()["count"] == 2

    def test_quality_avg_computes_mean(self, cm) -> None:
        """quality_avg computes the average across recorded scores."""
        s1 = ResponseScore(length_score=1.0, coherence_score=1.0, latency_score=1.0)
        s2 = ResponseScore(length_score=0.0, coherence_score=0.0, latency_score=0.0)
        cm.record_quality(s1)
        cm.record_quality(s2)
        avg = cm.quality_avg()
        assert avg["length"] == pytest.approx(0.5, abs=1e-4)
        assert avg["coherence"] == pytest.approx(0.5, abs=1e-4)
        assert avg["latency"] == pytest.approx(0.5, abs=1e-4)

    def test_to_dict_includes_quality_avg(self, cm) -> None:
        """to_dict includes quality_avg key."""
        d = cm.to_dict()
        assert "quality_avg" in d
        assert "count" in d["quality_avg"]

    def test_quality_persists_and_reloads(self, tmp_path: Path) -> None:
        """Quality sums persist to disk and reload correctly."""
        from skcapstone.metrics import ConsciousnessMetrics

        cm1 = ConsciousnessMetrics(home=tmp_path, persist_interval=0)
        score = score_response("capital of France?", "Paris is the capital.", 400.0)
        cm1.record_quality(score)
        cm1.save()

        cm2 = ConsciousnessMetrics(home=tmp_path, persist_interval=0)
        avg = cm2.quality_avg()
        assert avg["count"] == 1
        assert abs(avg["overall"] - score.overall) < 1e-4
