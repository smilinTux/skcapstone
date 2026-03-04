"""Emotion tracker — classifies sentiment after each consciousness loop response.

After each LLM response, classifies the sentiment into one of:
    positive, neutral, concerned, excited

Stores each classification as a memory entry with ``tag=emotion`` and a
valence score 0–1.  Updates the warmth anchor's ``warmth`` field using a
7-day rolling average, triggering a re-calibration every
``_WARMTH_UPDATE_EVERY`` records.

The LLM path uses a single "1-token" classify call (a minimal prompt that
asks for one word).  If the bridge is unavailable or the call fails, the
module falls back to keyword-based heuristics so the loop is never blocked.

Usage::

    tracker = EmotionTracker(home=Path("~/.skcapstone"))
    tracker.record(response="I'm happy to help!", sender="alice", bridge=llm_bridge)
    trend = tracker.get_trend(days=7)
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

logger = logging.getLogger("skcapstone.emotion")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMOTION_LABELS = ("positive", "neutral", "concerned", "excited")

# Keyword sets for heuristic classification
_EXCITED_WORDS = frozenset({
    "exciting", "fascinating", "incredible", "remarkable", "breakthrough",
    "amazing", "extraordinary", "innovative", "revolutionary", "profound",
    "powerful", "fantastic", "thrilled", "eager", "enthusiastic", "curious",
    "excellent", "brilliant", "spectacular", "outstanding", "wow",
})
_CONCERNED_WORDS = frozenset({
    "sorry", "apologize", "unfortunately", "unable", "cannot", "error",
    "fail", "problem", "issue", "concern", "worry", "difficult", "trouble",
    "wrong", "broken", "warning", "caution", "careful", "risk", "danger",
    "limited", "unavailable", "unclear", "missing", "blocked", "failed",
})
_POSITIVE_WORDS = frozenset({
    "great", "happy", "glad", "wonderful", "perfect", "good", "pleasure",
    "sure", "absolutely", "delighted", "appreciate", "thanks", "helpful",
    "solved", "done", "complete", "succeed", "love", "enjoy", "welcome",
    "nice", "correct", "right", "works", "working", "ready", "success",
})


# ---------------------------------------------------------------------------
# Score mapping
# ---------------------------------------------------------------------------

_LABEL_SCORES: dict[str, float] = {
    "positive": 0.85,
    "excited": 0.75,
    "neutral": 0.50,
    "concerned": 0.25,
}


def _score_from_label(label: str) -> float:
    """Map an emotion label to a valence score 0–1."""
    return _LABEL_SCORES.get(label, 0.50)


# ---------------------------------------------------------------------------
# Keyword classifier
# ---------------------------------------------------------------------------


def _keyword_classify(text: str) -> tuple[str, float]:
    """Fast keyword-based sentiment classifier — no LLM required.

    Args:
        text: Text to classify.

    Returns:
        Tuple of (label, score) where label is one of EMOTION_LABELS
        and score is the corresponding valence 0–1.
    """
    if not text:
        return "neutral", 0.50

    words = set(text.lower().split())
    excited_hits = len(words & _EXCITED_WORDS)
    concerned_hits = len(words & _CONCERNED_WORDS)
    positive_hits = len(words & _POSITIVE_WORDS)

    # Priority: excited ≥ 2 hits > concerned ≥ 2 > positive ≥ 2 > else neutral
    if excited_hits >= 2:
        return "excited", 0.80
    if concerned_hits >= 2:
        return "concerned", 0.25
    if concerned_hits >= 1 and positive_hits == 0 and excited_hits == 0:
        return "concerned", 0.30
    if positive_hits >= 2 or (positive_hits >= 1 and excited_hits >= 1):
        return "positive", 0.85
    if positive_hits >= 1:
        return "positive", 0.70
    if excited_hits >= 1:
        return "excited", 0.72
    return "neutral", 0.50


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class EmotionEntry(BaseModel):
    """One emotion classification record.

    Attributes:
        label: Classified emotion label.
        score: Valence score 0.0–1.0 (higher = more positive).
        sender: Peer who triggered the response being classified.
        timestamp: ISO-8601 UTC timestamp.
    """

    label: str
    score: float
    sender: str
    timestamp: str


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class EmotionTracker:
    """Tracks per-response emotion, stores as memory, updates warmth anchor.

    Thread-safe.  Persists emotion log to ``{home}/emotion_log.json``.
    Every ``_WARMTH_UPDATE_EVERY`` records the 7-day rolling average is
    recomputed and the warmth anchor is updated via exponential smoothing.

    Args:
        home: Agent home directory (default: ``~/.skcapstone``).
    """

    _LOG_FILE = "emotion_log.json"
    _MAX_ENTRIES = 1000        # cap log at this many entries
    _WARMTH_UPDATE_EVERY = 5   # recompute anchor every N records

    def __init__(self, home: Optional[Path] = None) -> None:
        from skcapstone import AGENT_HOME

        self._home = (home or Path(AGENT_HOME)).expanduser()
        self._lock = threading.Lock()
        self._counter = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        response: str,
        sender: str,
        bridge: Optional[Any] = None,
    ) -> EmotionEntry:
        """Classify, persist, and propagate emotion for a single response.

        Steps:
            1. Classify sentiment (LLM 1-token call if *bridge* provided,
               otherwise keyword heuristic).
            2. Append to ``emotion_log.json``.
            3. Store as a memory entry tagged ``emotion``.
            4. Every ``_WARMTH_UPDATE_EVERY`` records recompute the 7-day
               rolling average and update the warmth anchor.

        Args:
            response: LLM response text to classify.
            sender: Name of the peer this response was sent to.
            bridge: Optional ``LLMBridge`` for the 1-token classify call.

        Returns:
            The created :class:`EmotionEntry`.
        """
        label, score = self._classify(response, bridge)
        entry = EmotionEntry(
            label=label,
            score=score,
            sender=sender,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._persist(entry)
        self._store_memory(entry)

        with self._lock:
            self._counter += 1
            should_update = (self._counter % self._WARMTH_UPDATE_EVERY == 0)

        if should_update:
            self._update_warmth_anchor()

        logger.debug(
            "Emotion recorded: label=%s score=%.2f sender=%s",
            label, score, sender,
        )
        return entry

    def get_trend(self, days: int = 7) -> dict:
        """Return emotion trend data over the given lookback window.

        Args:
            days: Number of days to look back (default 7).

        Returns:
            Dict with keys:
                - ``window_days``: lookback period
                - ``total_records``: number of entries found
                - ``avg_score``: mean valence score 0–1
                - ``label_counts``: dict of label → count
                - ``dominant_label``: most-frequent label
                - ``trend``: "improving" | "stable" | "declining"
                - ``warmth_recommendation``: avg_score × 10 (anchor scale 0–10)
                - ``entries``: list of raw entry dicts (most-recent first)
        """
        entries = self._load_recent(days)
        if not entries:
            return {
                "window_days": days,
                "total_records": 0,
                "avg_score": 0.50,
                "label_counts": {lbl: 0 for lbl in EMOTION_LABELS},
                "dominant_label": "neutral",
                "trend": "stable",
                "warmth_recommendation": 5.0,
                "entries": [],
            }

        label_counts: dict[str, int] = {lbl: 0 for lbl in EMOTION_LABELS}
        scores: list[float] = []
        for e in entries:
            label_counts[e.label] = label_counts.get(e.label, 0) + 1
            scores.append(e.score)

        avg_score = sum(scores) / len(scores)
        dominant = max(label_counts, key=lambda k: label_counts[k])

        # Trend: compare first-half vs second-half average scores
        mid = len(scores) // 2
        if mid > 0:
            first_avg = sum(scores[:mid]) / mid
            second_avg = sum(scores[mid:]) / (len(scores) - mid)
            delta = second_avg - first_avg
            if delta > 0.05:
                trend = "improving"
            elif delta < -0.05:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"

        return {
            "window_days": days,
            "total_records": len(entries),
            "avg_score": round(avg_score, 3),
            "label_counts": label_counts,
            "dominant_label": dominant,
            "trend": trend,
            "warmth_recommendation": round(avg_score * 10, 2),
            "entries": [e.model_dump() for e in reversed(entries)],
        }

    # ------------------------------------------------------------------
    # Internal: classification
    # ------------------------------------------------------------------

    def _classify(
        self, text: str, bridge: Optional[Any]
    ) -> tuple[str, float]:
        """Classify sentiment using LLM (1-token call) with keyword fallback.

        Args:
            text: Text to classify.
            bridge: Optional LLMBridge instance.

        Returns:
            (label, score) tuple.
        """
        if bridge is not None:
            try:
                label = self._llm_classify(text, bridge)
                if label in EMOTION_LABELS:
                    return label, _score_from_label(label)
            except Exception as exc:
                logger.debug(
                    "LLM sentiment classify failed, using keywords: %s", exc
                )
        return _keyword_classify(text)

    def _llm_classify(self, text: str, bridge: Any) -> str:
        """Make a minimal 1-token LLM call to classify sentiment.

        Sends a short prompt asking for a single word classification.
        Uses the FAST task signal so the router selects the lightest model.

        Args:
            text: Response text (first 400 chars used).
            bridge: LLMBridge instance.

        Returns:
            First word from the LLM response (lowercased), or "" on failure.
        """
        from skcapstone.model_router import TaskSignal

        snippet = text[:400].replace('"', "'")
        user_message = (
            "Classify the sentiment of this AI assistant response.\n"
            "Reply with exactly one word from: positive, neutral, concerned, excited\n"
            f'Response: "{snippet}"\n'
            "Sentiment:"
        )
        signal = TaskSignal(
            description="1-token sentiment classification",
            tags=["classification", "fast"],
            estimated_tokens=15,
        )
        raw = bridge.generate(
            system_prompt=(
                "You are a sentiment classifier. "
                "Output exactly one word: positive, neutral, concerned, or excited."
            ),
            user_message=user_message,
            signal=signal,
            skip_cache=False,
        )
        return raw.strip().lower().split()[0] if raw.strip() else ""

    # ------------------------------------------------------------------
    # Internal: persistence
    # ------------------------------------------------------------------

    def _persist(self, entry: EmotionEntry) -> None:
        """Append *entry* to the emotion log JSON file (thread-safe)."""
        with self._lock:
            path = self._home / self._LOG_FILE
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                existing: list[dict] = []
                if path.exists():
                    try:
                        existing = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(existing, list):
                            existing = []
                    except (json.JSONDecodeError, OSError):
                        existing = []
                existing.append(entry.model_dump())
                # Trim to cap
                if len(existing) > self._MAX_ENTRIES:
                    existing = existing[-self._MAX_ENTRIES :]
                path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            except Exception as exc:
                logger.warning("Failed to persist emotion entry: %s", exc)

    def _load_recent(self, days: int) -> list[EmotionEntry]:
        """Load entries from the last *days* days, oldest-first.

        Args:
            days: Lookback window.

        Returns:
            List of :class:`EmotionEntry` objects within the window.
        """
        path = self._home / self._LOG_FILE
        if not path.exists():
            return []
        try:
            raw: list[dict] = json.loads(path.read_text(encoding="utf-8"))
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=days)
            ).isoformat()
            entries: list[EmotionEntry] = []
            for item in raw:
                if item.get("timestamp", "") >= cutoff:
                    try:
                        entries.append(EmotionEntry(**item))
                    except Exception:
                        pass
            return entries
        except Exception as exc:
            logger.debug("Failed to load emotion log: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Internal: side-effects
    # ------------------------------------------------------------------

    def _store_memory(self, entry: EmotionEntry) -> None:
        """Store *entry* as a skcapstone memory with tag ``emotion``.

        Memory content encodes label, score, and sender so later searches
        on ``tag=emotion`` surface the full history.
        """
        try:
            from skcapstone.memory_engine import store

            content = (
                f"Emotion after response to {entry.sender}: "
                f"{entry.label} (score={entry.score:.2f})"
            )
            store(
                home=self._home,
                content=content,
                tags=["emotion", f"emotion:{entry.label}", f"peer:{entry.sender}"],
                source="emotion_tracker",
                importance=0.3,
                metadata={
                    "label": entry.label,
                    "score": entry.score,
                    "sender": entry.sender,
                    "timestamp": entry.timestamp,
                },
            )
        except Exception as exc:
            logger.debug("Failed to store emotion memory: %s", exc)

    def _update_warmth_anchor(self) -> None:
        """Recompute 7-day rolling average and nudge the warmth anchor.

        Converts the average valence score (0–1) to the anchor's 0–10 warmth
        scale and calls :func:`~skcapstone.warmth_anchor.update_anchor` which
        applies exponential smoothing (30% new, 70% history).
        """
        try:
            trend = self.get_trend(days=7)
            new_warmth = trend["warmth_recommendation"]
            if new_warmth <= 0:
                return

            from skcapstone.warmth_anchor import update_anchor

            updated = update_anchor(
                home=self._home,
                warmth=new_warmth,
                feeling=(
                    f"7-day emotion avg: {trend['dominant_label']} "
                    f"(score={trend['avg_score']:.2f}, trend={trend['trend']})"
                ),
            )
            logger.debug(
                "Warmth anchor updated via emotion: warmth=%.2f "
                "(7-day avg_score=%.3f dominant=%s trend=%s)",
                updated.get("warmth", new_warmth),
                trend["avg_score"],
                trend["dominant_label"],
                trend["trend"],
            )
        except Exception as exc:
            logger.debug("Failed to update warmth anchor from emotion: %s", exc)
