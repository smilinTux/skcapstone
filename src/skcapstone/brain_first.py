"""
Brain-First Protocol — think before you act.

Before an agent acts on any task, it consults its memory to see if it
already knows something relevant.  This avoids redundant work, surfaces
prior decisions, and grounds the agent in its own experience.

Usage:
    from skcapstone.brain_first import brain_first_check

    result = brain_first_check("deploy the monitoring stack")
    if result.has_memories:
        # use result.memories as additional context
        ...

Configuration (config.yaml):
    brain_first:
      enabled: true          # master toggle (default: true)
      max_results: 5          # how many memories to surface (default: 5)
      min_importance: 0.3     # ignore low-importance memories (default: 0.3)
      auto_inject: false      # auto-prepend memories to MCP tool responses (default: false)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.brain_first")

# Stop-words to strip from queries before searching memory
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "out",
    "off", "over", "under", "again", "further", "then", "once", "here",
    "there", "when", "where", "why", "how", "all", "each", "every",
    "both", "few", "more", "most", "other", "some", "such", "no", "nor",
    "not", "only", "own", "same", "so", "than", "too", "very", "just",
    "because", "but", "and", "or", "if", "while", "about", "up", "it",
    "its", "this", "that", "these", "those", "i", "me", "my", "we",
    "our", "you", "your", "he", "him", "his", "she", "her", "they",
    "them", "their", "what", "which", "who", "whom",
})


@dataclass
class BrainFirstConfig:
    """Configuration for the brain-first protocol."""

    enabled: bool = True
    max_results: int = 5
    min_importance: float = 0.3
    auto_inject: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "BrainFirstConfig":
        """Create config from a dict (e.g. from config.yaml brain_first section)."""
        return cls(
            enabled=data.get("enabled", True),
            max_results=data.get("max_results", 5),
            min_importance=data.get("min_importance", 0.3),
            auto_inject=data.get("auto_inject", False),
        )


@dataclass
class BrainFirstResult:
    """Result of a brain-first memory consultation."""

    query: str
    keywords: list[str]
    memories: list[dict] = field(default_factory=list)
    enabled: bool = True
    error: Optional[str] = None

    @property
    def has_memories(self) -> bool:
        """Whether any relevant memories were found."""
        return len(self.memories) > 0

    def as_context(self) -> str:
        """Format memories as a context block for injection into prompts."""
        if not self.has_memories:
            return ""
        lines = ["[Brain-First: relevant memories found]"]
        for i, mem in enumerate(self.memories, 1):
            content = mem.get("content", "")[:200]
            layer = mem.get("layer", "?")
            importance = mem.get("importance", 0)
            tags = ", ".join(mem.get("tags", []))
            lines.append(
                f"  {i}. [{layer}|imp={importance:.1f}] {content}"
                + (f"  tags: {tags}" if tags else "")
            )
        return "\n".join(lines)


def extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from a text string.

    Strips stop-words and short tokens, keeping domain-relevant terms.

    Args:
        text: Input text (task title, prompt, etc.).

    Returns:
        List of unique keywords, longest first.
    """
    # Lowercase, split on non-alphanumeric
    tokens = re.split(r"[^a-zA-Z0-9_-]+", text.lower())
    # Filter: no stop-words, no short tokens
    keywords = list(dict.fromkeys(
        t for t in tokens if t and t not in _STOP_WORDS and len(t) > 2
    ))
    # Sort longest first (longer terms tend to be more specific)
    keywords.sort(key=len, reverse=True)
    return keywords


def _load_config() -> BrainFirstConfig:
    """Load brain-first config from the agent's config.yaml.

    Falls back to defaults if the file or section is missing.
    """
    try:
        import yaml
    except ImportError:
        return BrainFirstConfig()

    from . import AGENT_HOME, SKCAPSTONE_AGENT

    for base in [
        Path(AGENT_HOME).expanduser() / "agents" / SKCAPSTONE_AGENT,
        Path(AGENT_HOME).expanduser(),
    ]:
        config_file = base / "config" / "config.yaml"
        if config_file.exists():
            try:
                data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
                bf_data = data.get("brain_first", {})
                if bf_data:
                    return BrainFirstConfig.from_dict(bf_data)
            except Exception as exc:
                logger.debug("Failed to load brain_first config from %s: %s", config_file, exc)

    return BrainFirstConfig()


def brain_first_check(
    context: str,
    config: Optional[BrainFirstConfig] = None,
    tags: Optional[list[str]] = None,
) -> BrainFirstResult:
    """Consult memory before acting on a task.

    This is the core brain-first function.  Given a task description or
    prompt context, it extracts keywords, searches memory, and returns
    any relevant memories that the agent should consider.

    Args:
        context: The task description, prompt, or action context.
        config: Override config (uses agent config.yaml if None).
        tags: Optional tag filter for the memory search.

    Returns:
        BrainFirstResult with any relevant memories.
    """
    if config is None:
        config = _load_config()

    keywords = extract_keywords(context)
    result = BrainFirstResult(
        query=context,
        keywords=keywords,
        enabled=config.enabled,
    )

    if not config.enabled:
        result.error = "brain-first protocol disabled"
        return result

    if not keywords:
        result.error = "no meaningful keywords extracted"
        return result

    # Build a search query from top keywords (limit to 6 to avoid noise)
    search_query = " ".join(keywords[:6])

    try:
        from .memory_engine import search as memory_search
        from .mcp_tools._helpers import _home

        home = _home()
        entries = memory_search(
            home=home,
            query=search_query,
            tags=tags,
            limit=config.max_results * 2,  # over-fetch, then filter
        )

        # Filter by minimum importance
        entries = [e for e in entries if e.importance >= config.min_importance]

        # Truncate to max_results
        entries = entries[:config.max_results]

        result.memories = [
            {
                "memory_id": e.memory_id,
                "content": e.content[:300],
                "layer": e.layer.value,
                "tags": e.tags,
                "importance": e.importance,
                "access_count": e.access_count,
                "source": e.source,
            }
            for e in entries
        ]

        logger.info(
            "Brain-first check: %d memories found for %d keywords from '%s'",
            len(result.memories),
            len(keywords),
            context[:80],
        )

    except Exception as exc:
        result.error = f"memory search failed: {exc}"
        logger.warning("Brain-first check failed: %s", exc)

    return result
