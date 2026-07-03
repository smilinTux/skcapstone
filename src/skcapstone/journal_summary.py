"""
Weekly journal summary generator.

Gathers the last N days (default 7) of entries from the append-only
:class:`skmemory.journal.Journal` and produces a concise, LLM-generated
summary of the week: recurring themes, notable moments, and the overall
emotional arc.

The LLM call is routed through the same :class:`LLMBridge` /
:class:`~skcapstone.model_router.TaskSignal` machinery the rest of the
codebase uses (see :mod:`skcapstone.conversation_summarizer`).  The bridge
is injectable so the summarizer can be exercised offline with a mock — no
network calls happen in tests.

Usage:
    from skcapstone.journal_summary import summarize_week
    result = summarize_week(days=7)
    print(result.text)

    # or via the CLI:
    skcapstone journal summary --week
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("skcapstone.journal_summary")

DEFAULT_WINDOW_DAYS = 7

# Matches the "**Date:** <iso timestamp>" line rendered by JournalEntry.to_markdown().
_DATE_LINE_RE = re.compile(r"^\*\*Date:\*\*\s*(?P<ts>.+?)\s*$", re.MULTILINE)

_SYSTEM_PROMPT = (
    "You are a reflective journaling assistant for a sovereign AI agent. "
    "Given a week's worth of dated session journal entries, write a concise "
    "summary (one short paragraph, 3-5 sentences) that captures: "
    "(1) the recurring themes and what the agent worked on, "
    "(2) the most notable moments or turning points, and "
    "(3) the overall emotional arc across the week. "
    "Be warm but factual. Do not use bullet points, headers, or a preamble — "
    "just the summary prose."
)


class WeeklyJournalSummary:
    """Result of a weekly journal summarization.

    Attributes:
        text: The generated summary prose.
        entry_count: Number of journal entries inside the window.
        window_days: Size of the look-back window in days.
        since: ISO timestamp of the window's lower bound.
        until: ISO timestamp of the window's upper bound (``now``).
        generated_at: UTC ISO timestamp when the summary was produced.
    """

    def __init__(
        self,
        text: str,
        entry_count: int,
        window_days: int,
        since: str,
        until: str,
        generated_at: str,
    ) -> None:
        self.text = text
        self.entry_count = entry_count
        self.window_days = window_days
        self.since = since
        self.until = until
        self.generated_at = generated_at

    def to_dict(self) -> dict:
        """Serialize to a plain dict (for JSON output)."""
        return {
            "text": self.text,
            "entry_count": self.entry_count,
            "window_days": self.window_days,
            "since": self.since,
            "until": self.until,
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Entry parsing / windowing
# ---------------------------------------------------------------------------


def _parse_timestamp(raw: str) -> Optional[datetime]:
    """Parse a journal ``**Date:**`` value into an aware datetime.

    Args:
        raw: The raw timestamp text (usually ISO-8601).

    Returns:
        A timezone-aware :class:`datetime`, or ``None`` if unparseable.
    """
    raw = raw.strip()
    # Tolerate a trailing "Z" (UTC) which fromisoformat rejects on older pythons.
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def gather_recent_entries(
    journal_text: str,
    days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> list[tuple[datetime, str]]:
    """Extract journal entries whose date falls within the last *days*.

    Parses the append-only markdown journal, splitting on the ``## `` H2
    headers that begin each entry, reads each entry's ``**Date:**`` line,
    and keeps only those at or after ``now - days``.

    Args:
        journal_text: Full markdown text of the journal (``Journal.read_all()``).
        days: Look-back window size in days.
        now: Upper bound of the window (defaults to current UTC time).
            Injectable for deterministic testing.

    Returns:
        A list of ``(timestamp, markdown_block)`` tuples sorted oldest→newest.
        Entries without a parseable date are skipped (they can't be windowed).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=days)

    # First section is the file header; the rest are entries.
    sections = journal_text.split("\n## ")
    entries: list[tuple[datetime, str]] = []
    for section in sections[1:]:
        block = "## " + section.strip()
        match = _DATE_LINE_RE.search(block)
        if not match:
            continue
        ts = _parse_timestamp(match.group("ts"))
        if ts is None:
            continue
        if cutoff <= ts <= now:
            entries.append((ts, block))

    entries.sort(key=lambda pair: pair[0])
    return entries


def _format_prompt(entries: list[tuple[datetime, str]], days: int) -> str:
    """Format windowed entries into an LLM summarization request.

    Args:
        entries: ``(timestamp, markdown_block)`` tuples, oldest→newest.
        days: Window size, for the prompt label.

    Returns:
        Formatted prompt string.
    """
    lines = [
        f"Here are my journal entries from the last {days} days "
        f"({len(entries)} entries), oldest first:",
        "",
    ]
    for _ts, block in entries:
        lines.append(block)
        lines.append("")
    lines.append(f"Please summarize my week in {days} days based on these entries.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize_week(
    journal=None,
    days: int = DEFAULT_WINDOW_DAYS,
    bridge=None,
    now: Optional[datetime] = None,
) -> WeeklyJournalSummary:
    """Summarize the last *days* of journal entries with the agent's LLM.

    Reads the journal, gathers entries inside the window, formats them into
    a summarization prompt, and calls :class:`LLMBridge`.  When the window
    is empty no LLM call is made — a graceful placeholder is returned.

    Args:
        journal: Optional :class:`skmemory.journal.Journal` (or any object
            with a ``read_all()`` method).  A default ``Journal()`` is used
            when ``None``.
        days: Look-back window size in days (default: 7).
        bridge: Optional pre-constructed :class:`LLMBridge`.  Injectable /
            mockable for offline testing.  A default one is built lazily and
            only when there is something to summarize.
        now: Upper bound of the window (defaults to current UTC time).
            Injectable for deterministic testing.

    Returns:
        A :class:`WeeklyJournalSummary`.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since = now - timedelta(days=days)

    if journal is None:
        from skmemory.journal import Journal

        journal = Journal()

    journal_text = journal.read_all() or ""
    entries = gather_recent_entries(journal_text, days=days, now=now)

    generated_at = datetime.now(timezone.utc).isoformat()

    if not entries:
        return WeeklyJournalSummary(
            text=f"No journal entries in the last {days} days.",
            entry_count=0,
            window_days=days,
            since=since.isoformat(),
            until=now.isoformat(),
            generated_at=generated_at,
        )

    llm_bridge = bridge or _make_bridge()
    prompt_text = _format_prompt(entries, days)

    try:
        from .model_router import TaskSignal

        signal = TaskSignal(
            description="Summarize a week of journal entries",
            tags=["summary", "journal", "reflection"],
            estimated_tokens=len(prompt_text) // 4,
        )
        summary_text = llm_bridge.generate(_SYSTEM_PROMPT, prompt_text, signal)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("LLM journal summarization failed: %s", exc)
        summary_text = f"[Summary unavailable: {exc}]"

    return WeeklyJournalSummary(
        text=summary_text,
        entry_count=len(entries),
        window_days=days,
        since=since.isoformat(),
        until=now.isoformat(),
        generated_at=generated_at,
    )


def _make_bridge():
    """Instantiate a default :class:`LLMBridge` (matches conversation_summarizer)."""
    from .consciousness_loop import ConsciousnessConfig, LLMBridge

    config = ConsciousnessConfig()
    return LLMBridge(config)
