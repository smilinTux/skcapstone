"""Response quality scorer for consciousness loop LLM outputs.

Scores each LLM response on three dimensions:

- **length_score** (0-1): Is the response an appropriate length for the
  question? Too terse or too verbose both penalise this score.
- **coherence_score** (0-1): Do the meaningful keywords in the question
  appear in the response? High overlap → the model stayed on topic.
- **latency_score** (0-1): How fast was the response? Sub-500 ms scores
  1.0; latency above 30 s scores 0.1.

The **overall** score is a weighted average (coherence 40 %, length 30 %,
latency 30 %).

Usage::

    from skcapstone.response_scorer import score_response

    score = score_response(
        question="What is the capital of France?",
        response="The capital of France is Paris.",
        latency_ms=320.0,
    )
    print(score.overall)   # ~0.9
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Stopwords — filtered out before coherence keyword matching
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "not", "no", "so", "if", "then",
        "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "shall", "can",
        "to", "of", "in", "on", "at", "by", "for", "with", "about", "from",
        "as", "into", "that", "this", "these", "those",
        "i", "you", "he", "she", "it", "we", "they",
        "me", "him", "her", "us", "them",
        "my", "your", "his", "its", "our", "their",
        # Question / interrogative words (carry no domain meaning)
        "what", "when", "where", "who", "whom", "which", "why", "how",
    }
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ResponseScore:
    """Quality scores for a single LLM response.

    All scores are in the range [0.0, 1.0] where 1.0 is best.

    Attributes:
        length_score: Appropriateness of response length relative to question.
        coherence_score: Keyword overlap between question and response.
        latency_score: Speed rating; faster responses score higher.
        overall: Weighted average of the three dimension scores.
    """

    length_score: float
    coherence_score: float
    latency_score: float
    overall: float = field(init=False)

    def __post_init__(self) -> None:
        self.overall = round(
            self.coherence_score * 0.4
            + self.length_score * 0.3
            + self.latency_score * 0.3,
            4,
        )

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-serializable dict of all score dimensions.

        Returns:
            Dict with keys ``length``, ``coherence``, ``latency``, ``overall``.
        """
        return {
            "length": self.length_score,
            "coherence": self.coherence_score,
            "latency": self.latency_score,
            "overall": self.overall,
        }


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _score_length(question: str, response: str) -> float:
    """Score response length appropriateness.

    Computes an ideal word-count range from the question length and returns
    how well the response fits within that range.

    Args:
        question: The original question/prompt.
        response: The LLM response text.

    Returns:
        Float in [0.0, 1.0].
    """
    q_words = max(1, len(question.split()))
    r_words = len(response.split())

    if r_words == 0:
        return 0.0

    # Ideal range: [max(10, q_words), max(50, q_words * 10)]
    lo = max(10, q_words)
    hi = max(50, q_words * 10)

    if lo <= r_words <= hi:
        return 1.0
    if r_words < lo:
        # Linear ramp from 0 at r_words=0 up to 1 at r_words=lo
        return round(r_words / lo, 4)
    # r_words > hi — verbose, but penalise gently (verbose > silent)
    return round(max(0.3, hi / r_words), 4)


def _score_coherence(question: str, response: str) -> float:
    """Score coherence via keyword overlap.

    Extracts meaningful (non-stopword) words from the question and measures
    what fraction of them appear anywhere in the response.

    Args:
        question: The original question/prompt.
        response: The LLM response text.

    Returns:
        Float in [0.0, 1.0].  Returns 0.5 when the question has no
        meaningful keywords (i.e. nothing to measure against).
    """
    if not response.strip():
        return 0.0

    q_tokens = set(re.findall(r"\b\w+\b", question.lower())) - _STOPWORDS
    if not q_tokens:
        # No content words in question — treat as neutral
        return 0.5

    r_tokens = set(re.findall(r"\b\w+\b", response.lower()))
    overlap = q_tokens & r_tokens
    return round(len(overlap) / len(q_tokens), 4)


def _score_latency(latency_ms: float) -> float:
    """Score response latency.

    Uses a stepped scale so the typical interactive response window (under
    2 s) receives a high score while very slow responses (> 30 s) are
    significantly penalised.

    Args:
        latency_ms: Round-trip latency in milliseconds.

    Returns:
        Float in [0.1, 1.0].
    """
    if latency_ms <= 0:
        return 1.0
    if latency_ms <= 500:
        return 1.0
    if latency_ms <= 2_000:
        return 0.9
    if latency_ms <= 5_000:
        return 0.7
    if latency_ms <= 15_000:
        return 0.4
    if latency_ms <= 30_000:
        return 0.2
    return 0.1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_response(
    question: str,
    response: str,
    latency_ms: float,
) -> ResponseScore:
    """Score an LLM response on length, coherence, and latency.

    Args:
        question: The original question or user message sent to the LLM.
        response: The LLM's reply text.
        latency_ms: Total time from request to response in milliseconds.

    Returns:
        A :class:`ResponseScore` with individual dimension scores and an
        overall weighted score.

    Example::

        score = score_response("What is Python?", "Python is a language.", 800)
        assert 0.0 <= score.overall <= 1.0
    """
    length = _score_length(question, response)
    coherence = _score_coherence(question, response)
    latency = _score_latency(latency_ms)
    return ResponseScore(
        length_score=length,
        coherence_score=coherence,
        latency_score=latency,
    )
