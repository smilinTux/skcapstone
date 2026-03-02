"""Agent Mood Tracker — infers emotional state from interaction patterns.

Tracks three orthogonal mood dimensions derived from the consciousness
loop's runtime metrics:

  success_mood   — response success rate  (happy / content / neutral / frustrated)
  social_mood    — message frequency      (social / active / quiet / isolated)
  stress_mood    — error rate             (calm / relaxed / tense / stressed)

Persists to ``{home}/mood.json`` after every update so the state survives
daemon restarts and can be read by the CLI without touching the live daemon.

Usage::

    tracker = MoodTracker(home=Path("~/.skcapstone"))
    tracker.update(messages=42, responses=40, errors=2, window_hours=1)
    snap = tracker.snapshot
    print(snap.summary)        # "happy"
    print(tracker.describe())  # human-readable paragraph
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("skcapstone.mood")

_MOOD_FILE = "mood.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class MoodSnapshot(BaseModel):
    """Persisted mood snapshot.

    Attributes:
        messages_processed: Total messages seen in the tracking window.
        responses_sent: Total successful responses in the window.
        errors: Total errors in the window.
        success_rate: Fraction of messages responded to (0–1).
        error_rate: Fraction of messages that produced errors (0–1).
        messages_per_hour: Rolling message rate used for social classification.
        success_mood: Mood along the success axis.
        social_mood: Mood along the social-engagement axis.
        stress_mood: Mood along the error/stress axis.
        summary: Dominant single-word overall mood label.
        updated_at: ISO-8601 timestamp of last update.
        window_hours: Rolling window duration used for frequency calculation.
    """

    messages_processed: int = 0
    responses_sent: int = 0
    errors: int = 0

    # Derived rates
    success_rate: float = 1.0
    error_rate: float = 0.0
    messages_per_hour: float = 0.0

    # Classified states
    success_mood: str = "neutral"   # happy / content / neutral / frustrated
    social_mood: str = "quiet"      # social / active / quiet / isolated
    stress_mood: str = "calm"       # calm / relaxed / tense / stressed

    # Dominant label
    summary: str = "neutral"

    updated_at: str = ""
    window_hours: int = 24


# ---------------------------------------------------------------------------
# Axis classifiers
# ---------------------------------------------------------------------------


def _classify_success(success_rate: float) -> str:
    """Map a success rate to a mood label.

    Args:
        success_rate: Fraction of messages that received a response (0.0–1.0).

    Returns:
        One of ``"happy"``, ``"content"``, ``"neutral"``, ``"frustrated"``.
    """
    if success_rate >= 0.9:
        return "happy"
    if success_rate >= 0.7:
        return "content"
    if success_rate >= 0.5:
        return "neutral"
    return "frustrated"


def _classify_social(messages_per_hour: float) -> str:
    """Map message frequency to a social engagement label.

    Args:
        messages_per_hour: Rolling message rate.

    Returns:
        One of ``"social"``, ``"active"``, ``"quiet"``, ``"isolated"``.
    """
    if messages_per_hour >= 10.0:
        return "social"
    if messages_per_hour >= 3.0:
        return "active"
    if messages_per_hour >= 0.5:
        return "quiet"
    return "isolated"


def _classify_stress(error_rate: float) -> str:
    """Map an error rate to a stress level.

    Args:
        error_rate: Fraction of messages that produced errors (0.0–1.0).

    Returns:
        One of ``"calm"``, ``"relaxed"``, ``"tense"``, ``"stressed"``.
    """
    if error_rate < 0.05:
        return "calm"
    if error_rate < 0.15:
        return "relaxed"
    if error_rate < 0.30:
        return "tense"
    return "stressed"


def _compute_summary(success_mood: str, social_mood: str, stress_mood: str) -> str:
    """Derive a dominant single-word label from the three mood axes.

    Priority ordering: severe stress > frustration > mild stress >
    isolation > flourishing > happy > content > neutral.

    Args:
        success_mood: Label from :func:`_classify_success`.
        social_mood: Label from :func:`_classify_social`.
        stress_mood: Label from :func:`_classify_stress`.

    Returns:
        Single dominant mood label string.
    """
    if stress_mood == "stressed":
        return "stressed"
    if success_mood == "frustrated":
        return "frustrated"
    if stress_mood == "tense":
        return "tense"
    if social_mood == "isolated":
        return "isolated"
    if success_mood == "happy" and social_mood in ("social", "active"):
        return "flourishing"
    if success_mood == "happy":
        return "happy"
    if success_mood == "content":
        return "content"
    return "neutral"


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class MoodTracker:
    """Tracks and persists the agent's emotional state.

    Thread-safe. Persists to ``{home}/mood.json`` after every update so
    the CLI can read the state even when the daemon is not running.

    Args:
        home: Agent home directory (default: ``~/.skcapstone``).
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        from skcapstone import AGENT_HOME

        self._home = (home or Path(AGENT_HOME)).expanduser()
        self._lock = threading.Lock()
        self._snapshot = self._load() or MoodSnapshot()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def snapshot(self) -> MoodSnapshot:
        """Current mood snapshot (read-only copy)."""
        with self._lock:
            return self._snapshot.model_copy()

    def update(
        self,
        messages: int,
        responses: int,
        errors: int,
        window_hours: int = 24,
    ) -> MoodSnapshot:
        """Recompute mood from fresh counters and persist.

        Args:
            messages: Total messages processed in the tracking window.
            responses: Total successful responses in the window.
            errors: Total errors in the window.
            window_hours: Window duration used for frequency calculation.

        Returns:
            Updated :class:`MoodSnapshot`.
        """
        success_rate = responses / messages if messages > 0 else 1.0
        error_rate = errors / messages if messages > 0 else 0.0
        msgs_per_hour = messages / max(window_hours, 1)

        success_mood = _classify_success(success_rate)
        social_mood = _classify_social(msgs_per_hour)
        stress_mood = _classify_stress(error_rate)
        summary = _compute_summary(success_mood, social_mood, stress_mood)

        snap = MoodSnapshot(
            messages_processed=messages,
            responses_sent=responses,
            errors=errors,
            success_rate=round(success_rate, 4),
            error_rate=round(error_rate, 4),
            messages_per_hour=round(msgs_per_hour, 2),
            success_mood=success_mood,
            social_mood=social_mood,
            stress_mood=stress_mood,
            summary=summary,
            updated_at=datetime.now(timezone.utc).isoformat(),
            window_hours=window_hours,
        )
        with self._lock:
            self._snapshot = snap
        self._save(snap)
        return snap

    def update_from_metrics(self, metrics: object) -> MoodSnapshot:
        """Update mood using a ``ConsciousnessMetrics`` instance.

        Reads ``messages_processed``, ``responses_sent``, and ``errors``
        from the metrics object's ``to_dict()`` snapshot.

        Args:
            metrics: A :class:`~skcapstone.metrics.ConsciousnessMetrics` instance.

        Returns:
            Updated :class:`MoodSnapshot`.
        """
        try:
            d = metrics.to_dict()  # type: ignore[attr-defined]
            return self.update(
                messages=int(d.get("messages_processed", 0)),
                responses=int(d.get("responses_sent", 0)),
                errors=int(d.get("errors", 0)),
            )
        except Exception as exc:
            logger.warning("MoodTracker.update_from_metrics failed: %s", exc)
            with self._lock:
                return self._snapshot.model_copy()

    def describe(self) -> str:
        """Return a human-readable paragraph describing the current mood.

        Returns:
            Multi-line narrative string suitable for display.
        """
        snap = self.snapshot
        lines = [
            f"Mood summary : {snap.summary}",
            f"  Success  ({snap.success_mood:12s}): "
            f"{snap.responses_sent}/{snap.messages_processed} responses "
            f"({snap.success_rate:.0%} rate)",
            f"  Social   ({snap.social_mood:12s}): "
            f"{snap.messages_per_hour:.1f} msgs/hr  (window: {snap.window_hours}h)",
            f"  Stress   ({snap.stress_mood:12s}): "
            f"{snap.errors} errors  ({snap.error_rate:.0%} error rate)",
        ]
        if snap.updated_at:
            lines.append(f"  Updated  : {snap.updated_at[:19]}Z")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _mood_path(self) -> Path:
        """Return the absolute path to mood.json."""
        return self._home / _MOOD_FILE

    def _load(self) -> Optional[MoodSnapshot]:
        """Load persisted snapshot from disk.

        Returns:
            :class:`MoodSnapshot` on success, ``None`` on any error.
        """
        path = self._mood_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return MoodSnapshot(**data)
        except Exception as exc:
            logger.debug("Failed to load mood.json: %s", exc)
            return None

    def _save(self, snap: MoodSnapshot) -> None:
        """Atomically persist the snapshot to disk.

        Args:
            snap: The snapshot to write.
        """
        try:
            path = self._mood_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(snap.model_dump_json(indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save mood.json: %s", exc)

    @classmethod
    def load_snapshot(cls, home: Optional[Path] = None) -> MoodSnapshot:
        """Load the current mood snapshot without a full tracker instance.

        Useful for CLI display-only paths where no update is needed.

        Args:
            home: Agent home directory.

        Returns:
            :class:`MoodSnapshot` (default/neutral when file not found).
        """
        return cls(home=home).snapshot
