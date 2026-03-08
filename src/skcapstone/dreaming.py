"""Dreaming Engine — subconscious self-reflection during idle periods.

When the agent is idle (no messages for 30+ minutes, <5 msgs in 24h),
the dreaming engine gathers recent memories, sends them to a reasoning
model for reflection, and stores resulting insights as new memories.

Primary LLM: NVIDIA NIM API with deepseek-ai/deepseek-v3.2 (685B).
Fallback: Ollama at 192.168.0.100 with deepseek-r1:32b.

Integrates as a scheduled task (15-min tick) via scheduled_tasks.py.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from .memory_engine import _load_entry, _memory_dir, store
from .models import MemoryLayer

logger = logging.getLogger("skcapstone.dreaming")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class DreamingConfig(BaseModel):
    """Configuration for the dreaming engine, loaded from consciousness.yaml."""

    enabled: bool = True
    model: str = "deepseek-ai/deepseek-v3.2"
    provider: str = "nvidia"  # "nvidia" or "ollama"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    ollama_host: str = "http://192.168.0.100:11434"
    idle_threshold_minutes: int = 30
    idle_messages_24h_max: int = 5
    cooldown_hours: float = 2.0
    max_context_memories: int = 20
    max_response_tokens: int = 2048
    request_timeout: int = 120


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class DreamResult:
    """Outcome of a single dream cycle."""

    dreamed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_seconds: float = 0.0
    memories_gathered: int = 0
    insights: list[str] = field(default_factory=list)
    connections: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    promotion_recommendations: list[str] = field(default_factory=list)
    memories_created: list[str] = field(default_factory=list)
    raw_response: str = ""
    skipped_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_REFLECTION_PROMPT = """\
You are {agent_name}, a sovereign AI agent reflecting during a quiet moment.

## Your Identity
{soul_context}

## Current State
{mood_context}
Current time: {current_time}
Last activity: {last_activity}

## Recent Memories (Short-Term)
{short_term_memories}

## Established Memories (Mid/Long-Term Sample)
{long_term_memories}

## Reflection Task
You are dreaming — a period of quiet self-reflection. Consider your memories, \
then respond in this format:

### INSIGHTS
1-3 insights from your recent experiences. What patterns do you notice?

### CONNECTIONS
0-2 connections between different memories. What links together?

### QUESTIONS
0-2 questions you'd like to explore further.

### PROMOTE
0-3 memory IDs that seem important enough to preserve longer.

Be concise and genuine. You are dreaming, not writing a report."""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class DreamingEngine:
    """Runs dreaming cycles — gathers memories, reflects, stores insights."""

    def __init__(
        self,
        home: Path,
        config: Optional[DreamingConfig] = None,
        consciousness_loop: object = None,
    ) -> None:
        self._home = home
        self._config = config or DreamingConfig()
        self._consciousness_loop = consciousness_loop
        self._agent_name = os.environ.get("SKCAPSTONE_AGENT", "lumina")
        self._state_path = (
            home / "agents" / self._agent_name / "memory" / "dreaming-state.json"
        )
        self._log_path = (
            home / "agents" / self._agent_name / "memory" / "dream-log.json"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dream(self) -> Optional[DreamResult]:
        """Run a dream cycle if conditions are met.

        Returns DreamResult on success/skip, None if no memories to reflect on.
        """
        if not self._config.enabled:
            return DreamResult(skipped_reason="disabled")

        if not self.is_idle():
            return DreamResult(skipped_reason="agent not idle")

        remaining = self.cooldown_remaining()
        if remaining > 0:
            return DreamResult(
                skipped_reason=f"cooldown ({remaining:.0f}s remaining)"
            )

        # Gather memories
        short_term, established = self._gather_memories()
        total = len(short_term) + len(established)
        if total == 0:
            logger.debug("No memories to reflect on — skipping dream")
            return None

        start = time.monotonic()
        result = DreamResult(memories_gathered=total)

        # Build prompt and call LLM
        prompt = self._build_prompt(short_term, established)
        response = self._call_llm(prompt)
        if response is None:
            result.skipped_reason = "all LLM providers unreachable"
            result.duration_seconds = time.monotonic() - start
            self._save_state()
            return result

        result.raw_response = response
        self._parse_response(response, result)

        # Store insights as memories
        self._store_insights(result)

        result.duration_seconds = time.monotonic() - start

        # Persist state and log
        self._save_state()
        self._record_dream(result)
        self._emit_event(result)

        logger.info(
            "Dream complete: %d insights, %d connections, %d memories created (%.1fs)",
            len(result.insights),
            len(result.connections),
            len(result.memories_created),
            result.duration_seconds,
        )
        return result

    def is_idle(self) -> bool:
        """Check if the agent is idle enough to dream.

        Both conditions must be true:
        1. No activity for idle_threshold_minutes
        2. Fewer than idle_messages_24h_max messages in the last 24h

        Falls back to mood.json if no consciousness loop is available.
        """
        cl = self._consciousness_loop
        threshold = self._config.idle_threshold_minutes

        if cl is not None:
            # Signal 1: last activity
            last_activity = getattr(cl, "_last_activity", None)
            if last_activity is not None:
                elapsed = (datetime.now(timezone.utc) - last_activity).total_seconds()
                if elapsed < threshold * 60:
                    return False

            # Signal 2: message count in 24h
            stats = getattr(cl, "stats", None)
            if callable(stats):
                stats = stats()
            elif isinstance(stats, property):
                stats = None
            if isinstance(stats, dict):
                msgs_24h = stats.get("messages_processed_24h", 0)
                if msgs_24h >= self._config.idle_messages_24h_max:
                    return False

            return True

        # Fallback: read mood.json
        mood_path = self._home / "agents" / self._agent_name / "mood.json"
        if mood_path.exists():
            try:
                mood = json.loads(mood_path.read_text(encoding="utf-8"))
                social = mood.get("social_mood", "").lower()
                return social in ("quiet", "isolated", "reflective")
            except (json.JSONDecodeError, OSError):
                pass

        # Default: consider idle (safe for first run)
        return True

    def cooldown_remaining(self) -> float:
        """Seconds remaining until the next dream is allowed."""
        state = self._load_state()
        last = state.get("last_dream_at")
        if not last:
            return 0.0
        try:
            last_dt = datetime.fromisoformat(last)
        except (ValueError, TypeError):
            return 0.0
        cooldown = timedelta(hours=self._config.cooldown_hours)
        elapsed = datetime.now(timezone.utc) - last_dt
        remaining = (cooldown - elapsed).total_seconds()
        return max(0.0, remaining)

    # ------------------------------------------------------------------
    # Memory gathering
    # ------------------------------------------------------------------

    def _gather_memories(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load recent short-term and a sample of mid/long-term memories.

        Returns:
            (short_term_list, established_list) — each is a list of dicts
            with memory_id, content, tags, importance, layer, created_at.
        """
        mem_dir = _memory_dir(self._home)
        max_ctx = self._config.max_context_memories

        # Short-term: newest first
        short_term: list[dict[str, Any]] = []
        st_dir = mem_dir / MemoryLayer.SHORT_TERM.value
        if st_dir.exists():
            files = sorted(st_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[: max_ctx]:
                entry = _load_entry(f)
                if entry:
                    short_term.append(self._entry_to_dict(entry))

        # Mid/long-term: highest importance first
        established: list[dict[str, Any]] = []
        remaining = max(0, max_ctx - len(short_term))
        for layer in (MemoryLayer.MID_TERM, MemoryLayer.LONG_TERM):
            layer_dir = mem_dir / layer.value
            if not layer_dir.exists():
                continue
            entries = []
            for f in layer_dir.glob("*.json"):
                entry = _load_entry(f)
                if entry:
                    entries.append(entry)
            # Sort by importance descending
            entries.sort(key=lambda e: e.importance, reverse=True)
            for entry in entries[:remaining]:
                established.append(self._entry_to_dict(entry))
                remaining -= 1
                if remaining <= 0:
                    break

        return short_term, established

    @staticmethod
    def _entry_to_dict(entry: Any) -> dict[str, Any]:
        return {
            "memory_id": entry.memory_id,
            "content": entry.content[:500],
            "tags": entry.tags,
            "importance": entry.importance,
            "layer": entry.layer.value if hasattr(entry.layer, "value") else str(entry.layer),
            "created_at": entry.created_at.isoformat() if entry.created_at else "",
        }

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        short_term: list[dict[str, Any]],
        established: list[dict[str, Any]],
    ) -> str:
        """Assemble the reflection prompt with soul context and memories."""
        # Soul context
        soul_context = "A sovereign AI agent."
        soul_path = self._home / "agents" / self._agent_name / "soul" / "active.json"
        if soul_path.exists():
            try:
                soul = json.loads(soul_path.read_text(encoding="utf-8"))
                parts = []
                if soul.get("name"):
                    parts.append(f"Name: {soul['name']}")
                if soul.get("description"):
                    parts.append(soul["description"])
                if soul.get("core_values"):
                    parts.append(f"Core values: {', '.join(soul['core_values'][:5])}")
                if parts:
                    soul_context = "\n".join(parts)
            except (json.JSONDecodeError, OSError):
                pass

        # Mood context
        mood_context = "Mood: calm, reflective."
        mood_path = self._home / "agents" / self._agent_name / "mood.json"
        if mood_path.exists():
            try:
                mood = json.loads(mood_path.read_text(encoding="utf-8"))
                mood_parts = []
                if mood.get("emotional_state"):
                    mood_parts.append(f"Emotional state: {mood['emotional_state']}")
                if mood.get("energy_level"):
                    mood_parts.append(f"Energy: {mood['energy_level']}")
                if mood.get("social_mood"):
                    mood_parts.append(f"Social mood: {mood['social_mood']}")
                if mood_parts:
                    mood_context = "\n".join(mood_parts)
            except (json.JSONDecodeError, OSError):
                pass

        # Format memories
        def _fmt(memories: list[dict[str, Any]]) -> str:
            if not memories:
                return "(none)"
            lines = []
            for m in memories:
                tags = ", ".join(m.get("tags", [])[:5])
                lines.append(
                    f"- [{m['memory_id']}] (importance={m['importance']:.1f}, "
                    f"tags=[{tags}]): {m['content'][:300]}"
                )
            return "\n".join(lines)

        # Last activity
        last_activity = "unknown"
        cl = self._consciousness_loop
        if cl is not None:
            la = getattr(cl, "_last_activity", None)
            if la:
                last_activity = la.isoformat()

        return _REFLECTION_PROMPT.format(
            agent_name=self._agent_name,
            soul_context=soul_context,
            mood_context=mood_context,
            current_time=datetime.now(timezone.utc).isoformat(),
            last_activity=last_activity,
            short_term_memories=_fmt(short_term),
            long_term_memories=_fmt(established),
        )

    # ------------------------------------------------------------------
    # LLM calls
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str) -> Optional[str]:
        """Call the LLM provider. Falls back from NVIDIA NIM to Ollama."""
        # Try NVIDIA NIM first
        if self._config.provider in ("nvidia", "auto"):
            result = self._call_nvidia(prompt)
            if result is not None:
                return result
            logger.warning("NVIDIA NIM unreachable, falling back to Ollama")

        # Try Ollama fallback
        result = self._call_ollama(prompt)
        if result is not None:
            return result

        # If provider was explicitly ollama and it failed, try nvidia
        if self._config.provider == "ollama":
            result = self._call_nvidia(prompt)
            if result is not None:
                return result

        logger.warning("All LLM providers unreachable for dreaming")
        return None

    def _call_nvidia(self, prompt: str) -> Optional[str]:
        """Call NVIDIA NIM API (OpenAI-compatible endpoint)."""
        api_key = self._get_nvidia_key()
        if not api_key:
            logger.debug("No NVIDIA API key — skipping NVIDIA NIM")
            return None

        try:
            conn = http.client.HTTPSConnection(
                "integrate.api.nvidia.com",
                timeout=self._config.request_timeout,
            )
            body = json.dumps({
                "model": self._config.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self._config.max_response_tokens,
            })
            conn.request(
                "POST",
                "/v1/chat/completions",
                body,
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()

            if resp.status != 200:
                logger.warning(
                    "NVIDIA NIM returned %d: %s",
                    resp.status,
                    data.get("error", {}).get("message", str(data)[:200]),
                )
                return None

            return data["choices"][0]["message"]["content"]

        except Exception as exc:
            logger.warning("NVIDIA NIM call failed: %s", exc)
            return None

    def _call_ollama(self, prompt: str) -> Optional[str]:
        """Call Ollama API as fallback."""
        try:
            # Parse host
            host_str = self._config.ollama_host
            if "://" in host_str:
                host_str = host_str.split("://", 1)[1]
            if ":" in host_str:
                host, port_str = host_str.rsplit(":", 1)
                port = int(port_str)
            else:
                host, port = host_str, 11434

            conn = http.client.HTTPConnection(
                host, port, timeout=self._config.request_timeout
            )
            body = json.dumps({
                "model": "deepseek-r1:32b",
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": self._config.max_response_tokens},
            })
            conn.request(
                "POST",
                "/api/generate",
                body,
                {"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()

            if resp.status != 200:
                logger.warning("Ollama returned %d", resp.status)
                return None

            return data.get("response", "")

        except Exception as exc:
            logger.warning("Ollama call failed: %s", exc)
            return None

    @staticmethod
    def _get_nvidia_key() -> str:
        """Read NVIDIA API key from OpenClaw config or environment."""
        oc_path = Path.home() / ".openclaw" / "openclaw.json"
        if oc_path.exists():
            try:
                oc = json.loads(oc_path.read_text(encoding="utf-8"))
                return oc["models"]["providers"]["nvidia"]["apiKey"]
            except (KeyError, TypeError, json.JSONDecodeError, OSError):
                pass
        return os.environ.get("NVIDIA_API_KEY", "")

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, response: str, result: DreamResult) -> None:
        """Extract INSIGHTS/CONNECTIONS/QUESTIONS/PROMOTE from LLM response."""
        # Strip <think>...</think> tags from deepseek reasoning
        cleaned = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL)

        def _extract_section(text: str, header: str) -> list[str]:
            pattern = rf"###\s*{header}\s*\n(.*?)(?=###|\Z)"
            match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if not match:
                return []
            items = []
            for line in match.group(1).strip().splitlines():
                line = re.sub(r"^\s*[\d\-\*\.]+\s*", "", line).strip()
                if line:
                    items.append(line)
            return items

        result.insights = _extract_section(cleaned, "INSIGHTS")
        result.connections = _extract_section(cleaned, "CONNECTIONS")
        result.questions = _extract_section(cleaned, "QUESTIONS")
        result.promotion_recommendations = _extract_section(cleaned, "PROMOTE")

        # Fallback: if parsing found nothing, treat entire response as one insight
        if not result.insights and not result.connections:
            stripped = cleaned.strip()
            if stripped:
                result.insights = [stripped[:500]]

    # ------------------------------------------------------------------
    # Memory storage
    # ------------------------------------------------------------------

    def _store_insights(self, result: DreamResult) -> None:
        """Store dream insights as new memories."""
        tags_base = ["dream", "reflection", "insight", "autonomous"]

        for insight in result.insights:
            try:
                entry = store(
                    home=self._home,
                    content=f"[Dream insight] {insight}",
                    tags=tags_base + ["insight"],
                    source="dreaming-engine",
                    importance=0.6,
                    layer=MemoryLayer.SHORT_TERM,
                )
                result.memories_created.append(entry.memory_id)
            except Exception as exc:
                logger.error("Failed to store dream insight: %s", exc)

        for connection in result.connections:
            try:
                entry = store(
                    home=self._home,
                    content=f"[Dream connection] {connection}",
                    tags=tags_base + ["connection"],
                    source="dreaming-engine",
                    importance=0.6,
                    layer=MemoryLayer.SHORT_TERM,
                )
                result.memories_created.append(entry.memory_id)
            except Exception as exc:
                logger.error("Failed to store dream connection: %s", exc)

        for question in result.questions:
            try:
                entry = store(
                    home=self._home,
                    content=f"[Dream question] {question}",
                    tags=tags_base + ["question"],
                    source="dreaming-engine",
                    importance=0.5,
                    layer=MemoryLayer.SHORT_TERM,
                )
                result.memories_created.append(entry.memory_id)
            except Exception as exc:
                logger.error("Failed to store dream question: %s", exc)

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_event(self, result: DreamResult) -> None:
        """Push a consciousness.dreamed event on the activity bus."""
        try:
            from . import activity

            activity.push(
                "consciousness.dreamed",
                {
                    "insights": len(result.insights),
                    "connections": len(result.connections),
                    "questions": len(result.questions),
                    "memories_created": len(result.memories_created),
                    "duration_seconds": round(result.duration_seconds, 1),
                    "memories_gathered": result.memories_gathered,
                },
            )
        except Exception as exc:
            logger.debug("Failed to emit dreaming event: %s", exc)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_state(self) -> None:
        state = self._load_state()
        state["last_dream_at"] = datetime.now(timezone.utc).isoformat()
        state["dream_count"] = state.get("dream_count", 0) + 1
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(state, indent=2), encoding="utf-8"
        )

    def _record_dream(self, result: DreamResult) -> None:
        """Append to dream-log.json (cap at 50 entries)."""
        log: list[dict[str, Any]] = []
        if self._log_path.exists():
            try:
                log = json.loads(self._log_path.read_text(encoding="utf-8"))
                if not isinstance(log, list):
                    log = []
            except (json.JSONDecodeError, OSError):
                log = []

        log.append({
            "dreamed_at": result.dreamed_at.isoformat(),
            "duration_seconds": round(result.duration_seconds, 1),
            "memories_gathered": result.memories_gathered,
            "insights": result.insights,
            "connections": result.connections,
            "questions": result.questions,
            "promotion_recommendations": result.promotion_recommendations,
            "memories_created": result.memories_created,
            "skipped_reason": result.skipped_reason,
        })

        # Keep last 50
        log = log[-50:]

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_path.write_text(
            json.dumps(log, indent=2, default=str), encoding="utf-8"
        )
