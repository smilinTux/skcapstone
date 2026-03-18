"""
Consciousness Loop — autonomous agent message processing.

Watches the SKComm inbox for incoming messages, classifies them,
routes to the appropriate LLM via the model router, and sends
responses back through SKComm. Self-heals when backends go down
by cascading through fallback providers.

Architecture:
    InboxHandler        — watchdog inotify handler for sub-second trigger
    ConsciousnessConfig — Pydantic configuration
    LLMBridge           — connects model router to skseed callbacks
    SystemPromptBuilder — assembles agent context for LLM system prompt
    ConsciousnessLoop   — the core orchestrator
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from skcapstone.blueprints.schema import ModelTier
from skcapstone.conversation_manager import ConversationManager
from skcapstone.conversation_store import ConversationStore
from skcapstone.fallback_tracker import FallbackEvent, FallbackTracker
from skcapstone.metrics import ConsciousnessMetrics
from skcapstone.model_router import ModelRouter, ModelRouterConfig, RouteDecision, TaskSignal
from skcapstone.prompt_adapter import AdaptedPrompt, PromptAdapter
from skcapstone.response_cache import ResponseCache, hash_prompt

logger = logging.getLogger("skcapstone.consciousness")

# Default inbox path under shared root
_INBOX_DIR = "sync/comms/inbox"

# Allowlist for peer name characters (alphanumeric + safe punctuation, no path separators)
_PEER_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-@\.]")


def _sanitize_peer_name(peer: str) -> str:
    """Sanitize a peer name for safe use as a filesystem key.

    Strips path separators (/ \\), null bytes, and any character not in the
    alphanumeric + ``-_@.`` set.  Caps length at 64 characters.  Returns
    ``"unknown"`` if the result would be empty.

    This prevents path-traversal attacks where an attacker crafts a sender
    field such as ``"../../../etc/passwd"`` to write outside the conversations
    directory.

    Args:
        peer: Raw peer name from an incoming message envelope.

    Returns:
        Filesystem-safe peer name, at most 64 characters long.
    """
    if not peer or not isinstance(peer, str):
        return "unknown"
    # Drop null bytes and path separators before the character-class filter
    sanitized = peer.replace("\x00", "").replace("/", "").replace("\\", "")
    sanitized = _PEER_NAME_SAFE_RE.sub("", sanitized)
    # Trim leading/trailing dots to avoid hidden-file or relative-ref confusion
    sanitized = sanitized.strip(".")
    return sanitized[:64] or "unknown"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConsciousnessConfig(BaseModel):
    """Configuration for the consciousness loop."""

    enabled: bool = True
    use_inotify: bool = True
    inotify_debounce_ms: int = 200
    response_timeout: int = 120
    max_context_tokens: int = 8000
    max_history_messages: int = 10
    auto_memory: bool = True
    auto_ack: bool = True
    privacy_default: bool = False
    max_concurrent_requests: int = 3
    fallback_chain: list[str] = Field(
        default_factory=lambda: [
            "ollama", "grok", "kimi", "nvidia", "anthropic", "openai", "passthrough",
        ]
    )
    desktop_notifications: bool = True


# ---------------------------------------------------------------------------
# Backend inference helper
# ---------------------------------------------------------------------------

_OLLAMA_MODEL_PATTERNS = (
    "llama", "mistral", "nemotron", "devstral",
    "deepseek", "qwen", "codestral",
)


def _backend_from_model(model_name: str, tier: ModelTier) -> str:
    """Infer the backend provider from a model name and routing tier.

    Mirrors the pattern-matching logic in :meth:`LLMBridge._resolve_callback`
    so callers can record which backend was actually used.

    Args:
        model_name: Concrete model name (e.g. ``"claude-3-5-sonnet-20241022"``).
        tier: The :class:`ModelTier` used for this request.

    Returns:
        Backend string: ``"ollama"``, ``"anthropic"``, ``"openai"``, ``"grok"``,
        ``"kimi"``, ``"nvidia"``, ``"passthrough"``, or ``"unknown"``.
    """
    if tier == ModelTier.LOCAL:
        return "ollama"
    name_base = model_name.lower().split(":")[0]
    if "claude" in name_base:
        return "anthropic"
    if any(x in name_base for x in ("gpt", "o1", "o3", "o4")):
        return "openai"
    if "grok" in name_base:
        return "grok"
    if "kimi" in name_base or "moonshot" in name_base:
        return "kimi"
    if "nvidia" in name_base:
        return "nvidia"
    if any(p in name_base for p in _OLLAMA_MODEL_PATTERNS):
        return "ollama"
    return "unknown"


# ---------------------------------------------------------------------------
# Ollama Connection Pool
# ---------------------------------------------------------------------------


class _OllamaPool:
    """Thread-safe HTTP connection pool for the Ollama REST API.

    Keeps a single persistent :class:`http.client.HTTPConnection` alive and
    reuses it across health probes.  The connection is transparently
    recreated after *ttl* seconds or after any network error so callers
    never see a stale socket.

    Args:
        host: Full Ollama base URL, e.g. ``http://localhost:11434``.
        ttl:  Seconds to keep the connection alive before recycling.
              Defaults to 60.
    """

    def __init__(self, host: str, ttl: int = 60) -> None:
        parsed = urlparse(host)
        self._host: str = parsed.hostname or "localhost"
        self._port: int = parsed.port or 11434
        self._ttl: int = ttl
        self._conn: Optional[http.client.HTTPConnection] = None
        self._created_at: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self) -> http.client.HTTPConnection:
        """Return a live connection, creating one when stale or absent."""
        with self._lock:
            if not self._is_valid():
                self._close_locked()
                self._conn = http.client.HTTPConnection(
                    self._host, self._port, timeout=2
                )
                self._created_at = time.monotonic()
            return self._conn  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Close and discard the cached connection (call after any error)."""
        with self._lock:
            self._close_locked()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_valid(self) -> bool:
        """True when a cached connection exists and is within its TTL."""
        return (
            self._conn is not None
            and (time.monotonic() - self._created_at) < self._ttl
        )

    def _close_locked(self) -> None:
        """Close the underlying socket.  Must be called with *self._lock* held."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                logger.warning("Failed to close connection socket: %s", exc)
            self._conn = None
            self._created_at = 0.0


# ---------------------------------------------------------------------------
# LLM Bridge
# ---------------------------------------------------------------------------


class LLMBridge:
    """Connects model router decisions to skseed LLM callbacks.

    Probes available backends, routes via ModelRouter, and cascades
    through fallbacks on failure.

    Args:
        config: Consciousness configuration.
        router_config: Optional custom model router config.
        adapter: Optional PromptAdapter for per-model formatting.
        cache: Optional ResponseCache.  When provided, generate() checks the
            cache before calling an LLM and stores successful results.
    """

    def __init__(
        self,
        config: ConsciousnessConfig,
        router_config: Optional[ModelRouterConfig] = None,
        adapter: Optional[PromptAdapter] = None,
        cache: Optional[ResponseCache] = None,
    ) -> None:
        self._router = ModelRouter(config=router_config)
        self._adapter = adapter or PromptAdapter()
        self._fallback_chain = config.fallback_chain
        self._timeout = config.response_timeout
        self._available: dict[str, bool] = {}
        self._cache: Optional[ResponseCache] = cache
        self._fallback_tracker = FallbackTracker()
        self._ollama_pool = _OllamaPool(
            os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        )
        self._probe_available_backends()

    def _probe_available_backends(self) -> None:
        """Probe all backends for availability."""
        self._available = {
            "ollama": self._probe_ollama(),
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "openai": bool(os.environ.get("OPENAI_API_KEY")),
            "grok": bool(os.environ.get("XAI_API_KEY")),
            "kimi": bool(os.environ.get("MOONSHOT_API_KEY")),
            "nvidia": bool(os.environ.get("NVIDIA_API_KEY")),
            "passthrough": True,
        }
        available = [k for k, v in self._available.items() if v]
        logger.info("LLM backends available: %s", available)

    def _probe_ollama(self) -> bool:
        """Check if Ollama is reachable, reusing the connection pool."""
        try:
            conn = self._ollama_pool.get()
            conn.request("GET", "/api/tags")
            resp = conn.getresponse()
            resp.read()  # drain body so the connection stays reusable
            return resp.status < 500
        except Exception:
            self._ollama_pool.invalidate()
            return False

    def _resolve_callback(self, tier: ModelTier, model_name: str):
        """Map tier+model to a skseed callback.

        Args:
            tier: The routing tier.
            model_name: The concrete model name.

        Returns:
            An LLMCallback callable.
        """
        from skseed.llm import (
            anthropic_callback,
            grok_callback,
            kimi_callback,
            nvidia_callback,
            ollama_callback,
            openai_callback,
            passthrough_callback,
        )

        name_lower = model_name.lower()
        # Strip Ollama :tag suffix for pattern matching (e.g. "deepseek-r1:8b" -> "deepseek-r1")
        name_base = name_lower.split(":")[0]

        # LOCAL tier always goes to Ollama
        if tier == ModelTier.LOCAL:
            return ollama_callback(model=model_name)

        # Pattern matching on model name (use name_base to handle :tag suffixes)
        if "claude" in name_base:
            return anthropic_callback(model=model_name)
        if "gpt" in name_base or "o1" in name_base or "o3" in name_base or "o4" in name_base:
            return openai_callback(model=model_name)
        if "grok" in name_base:
            return grok_callback(model=model_name)
        if "kimi" in name_base or "moonshot" in name_base:
            return kimi_callback(model=model_name)
        if "nvidia" in name_base:
            return nvidia_callback(model=model_name)

        # Models that run on Ollama (local inference)
        ollama_patterns = (
            "llama", "mistral", "nemotron", "devstral",
            "deepseek", "qwen", "codestral",
        )
        for pattern in ollama_patterns:
            if pattern in name_base:
                return ollama_callback(model=model_name)

        # Walk fallback chain for first available backend
        for backend in self._fallback_chain:
            if not self._available.get(backend, False):
                continue
            if backend == "ollama":
                return ollama_callback(model="llama3.2")
            elif backend == "anthropic":
                return anthropic_callback()
            elif backend == "openai":
                return openai_callback()
            elif backend == "grok":
                return grok_callback()
            elif backend == "kimi":
                return kimi_callback()
            elif backend == "nvidia":
                return nvidia_callback()
            elif backend == "passthrough":
                return self._make_passthrough_callback()

        return self._make_passthrough_callback()

    @staticmethod
    def _make_passthrough_callback():
        """Return a passthrough callback that always produces a plain str.

        The skseed passthrough_callback() expects a str, but generate() passes
        an AdaptedPrompt object.  This wrapper extracts the user message content
        from AdaptedPrompt so the callback never raises a TypeError or hangs.

        Returns:
            Callable that accepts str or AdaptedPrompt and returns str.
        """
        from skseed.llm import passthrough_callback
        _pt = passthrough_callback()

        def _wrapper(prompt):
            if hasattr(prompt, "messages"):
                # Extract user message from AdaptedPrompt
                for msg in prompt.messages:
                    if msg.get("role") == "user":
                        return str(msg.get("content", ""))
                return str(prompt)
            return _pt(str(prompt))

        return _wrapper

    def _tier_timeout(self, tier: ModelTier) -> int:
        """Return response timeout in seconds for the given tier.

        FAST and LOCAL are 180s because the machine runs CPU-only inference
        (Intel i7, no GPU) and even llama3.2 (3.2B) takes 60-180s.

        Returns:
            Seconds: FAST=180, CODE=300, REASON=300, NUANCE=180, LOCAL=180,
            default=120.
        """
        _map = {
            ModelTier.FAST: 180,
            ModelTier.CODE: 300,
            ModelTier.REASON: 300,
            ModelTier.NUANCE: 180,
            ModelTier.LOCAL: 180,
        }
        return _map.get(tier, 120)

    def _timed_call(self, callback, prompt: Any, tier: ModelTier) -> str:
        """Execute a callback with a tier-appropriate timeout.

        Uses a single-worker ThreadPoolExecutor so the calling thread is
        never blocked indefinitely. On timeout, the background thread is
        abandoned (not cancellable) and a TimeoutError propagates to the
        caller so it can continue to the next fallback.

        Args:
            callback: LLM callback to invoke.
            prompt: Prompt (str or AdaptedPrompt) to pass to the callback.
            tier: Model tier used to select the timeout.

        Returns:
            LLM response string.

        Raises:
            concurrent.futures.TimeoutError: If the call exceeds the limit.
            Exception: Any other exception raised by the callback.
        """
        timeout = self._tier_timeout(tier)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(callback, prompt)
            return future.result(timeout=timeout)
        finally:
            executor.shutdown(wait=False)

    def generate(
        self,
        system_prompt: str,
        user_message: str,
        signal: TaskSignal,
        _out_info: Optional[dict] = None,
        skip_cache: bool = False,
    ) -> str:
        """Route via ModelRouter, adapt prompt, call LLM, cascade on failure.

        Args:
            system_prompt: The agent's system context.
            user_message: The incoming message to respond to.
            signal: Task classification signal.
            _out_info: Optional dict populated with ``backend`` and ``tier``
                keys indicating which provider served the request.
            skip_cache: When True, bypass the response cache entirely.  Set
                this for real-time conversation messages whose system prompt
                embeds dynamic peer history that changes per exchange.

        Returns:
            LLM response text, or a fallback error message.
        """
        from skseed.llm import (
            anthropic_callback,
            grok_callback,
            kimi_callback,
            nvidia_callback,
            ollama_callback,
            openai_callback,
        )

        decision = self._router.route(signal)
        logger.info(
            "Routed to tier=%s model=%s: %s",
            decision.tier.value, decision.model_name, decision.reasoning,
        )

        # Cache look-up (before any LLM call)
        _prompt_hash: Optional[str] = None
        if self._cache is not None and not skip_cache:
            _prompt_hash = hash_prompt(system_prompt, user_message)
            cached = self._cache.get(_prompt_hash, decision.model_name)
            if cached is not None:
                logger.info("Cache hit — skipping LLM call (model=%s)", decision.model_name)
                if _out_info is not None:
                    _out_info["backend"] = "cache"
                    _out_info["tier"] = decision.tier.value
                return cached

        # For FAST tier (CPU-only Ollama), truncate system prompt to ~2000 chars
        # so the model spends its cycles on the response, not processing a giant context.
        if decision.tier == ModelTier.FAST and len(system_prompt) > 2000:
            system_prompt = system_prompt[:2000] + "..."
            logger.debug("FAST tier: system prompt truncated to 2000 chars")

        # Adapt prompt for the target model
        adapted = self._adapter.adapt(
            system_prompt, user_message,
            decision.model_name, decision.tier,
        )
        logger.debug(
            "Prompt adapted: profile=%s adaptations=%s",
            adapted.profile_used, adapted.adaptations_applied,
        )

        # Capture primary model identity for fallback tracking
        _primary_model = decision.model_name
        _primary_backend = _backend_from_model(decision.model_name, decision.tier)

        # Try primary model
        try:
            callback = self._resolve_callback(decision.tier, decision.model_name)
            result = self._timed_call(callback, adapted, decision.tier)
            if _out_info is not None:
                _out_info["backend"] = _primary_backend
                _out_info["tier"] = decision.tier.value
            if self._cache is not None and not skip_cache and _prompt_hash is not None:
                self._cache.put(_prompt_hash, decision.model_name, decision.tier, result)
            return result
        except Exception as exc:
            logger.warning(
                "Primary model %s failed: %s", decision.model_name, exc
            )

        # Try alternate models in same tier
        tier_models = self._router.config.tier_models.get(decision.tier.value, [])
        for alt_model in tier_models[1:]:
            alt_backend = _backend_from_model(alt_model, decision.tier)
            try:
                logger.info("Trying alt model: %s", alt_model)
                alt_adapted = self._adapter.adapt(
                    system_prompt, user_message, alt_model, decision.tier,
                )
                callback = self._resolve_callback(decision.tier, alt_model)
                result = self._timed_call(callback, alt_adapted, decision.tier)
                if _out_info is not None:
                    _out_info["backend"] = alt_backend
                    _out_info["tier"] = decision.tier.value
                self._fallback_tracker.record(FallbackEvent(
                    primary_model=_primary_model,
                    primary_backend=_primary_backend,
                    fallback_model=alt_model,
                    fallback_backend=alt_backend,
                    reason=f"primary model {_primary_model!r} failed; trying same-tier alt",
                    success=True,
                ))
                return result
            except Exception as exc:
                logger.warning("Alt model %s failed: %s", alt_model, exc)
                self._fallback_tracker.record(FallbackEvent(
                    primary_model=_primary_model,
                    primary_backend=_primary_backend,
                    fallback_model=alt_model,
                    fallback_backend=alt_backend,
                    reason=f"primary model {_primary_model!r} failed; alt {alt_model!r} also failed: {exc}",
                    success=False,
                ))

        # Tier downgrade: try FAST tier
        if decision.tier != ModelTier.FAST:
            fast_models = self._router.config.tier_models.get(ModelTier.FAST.value, [])
            for fast_model in fast_models:
                fast_backend = _backend_from_model(fast_model, ModelTier.FAST)
                try:
                    logger.info("Downgrading to FAST tier: %s", fast_model)
                    fast_adapted = self._adapter.adapt(
                        system_prompt, user_message, fast_model, ModelTier.FAST,
                    )
                    callback = self._resolve_callback(ModelTier.FAST, fast_model)
                    result = self._timed_call(callback, fast_adapted, ModelTier.FAST)
                    if _out_info is not None:
                        _out_info["backend"] = fast_backend
                        _out_info["tier"] = ModelTier.FAST.value
                    self._fallback_tracker.record(FallbackEvent(
                        primary_model=_primary_model,
                        primary_backend=_primary_backend,
                        fallback_model=fast_model,
                        fallback_backend=fast_backend,
                        reason=f"tier downgrade: {decision.tier.value} exhausted; using FAST model {fast_model!r}",
                        success=True,
                    ))
                    return result
                except Exception as exc:
                    logger.warning("FAST model %s failed: %s", fast_model, exc)
                    self._fallback_tracker.record(FallbackEvent(
                        primary_model=_primary_model,
                        primary_backend=_primary_backend,
                        fallback_model=fast_model,
                        fallback_backend=fast_backend,
                        reason=f"tier downgrade: FAST model {fast_model!r} failed: {exc}",
                        success=False,
                    ))

        # Cross-provider cascade via fallback chain — direct backend mapping,
        # no _resolve_callback, to avoid infinite regression on unknown names.
        for backend in self._fallback_chain:
            if not self._available.get(backend, False):
                continue
            try:
                logger.info("Fallback cascade: %s", backend)
                if backend == "ollama":
                    callback = ollama_callback(model="llama3.2")
                elif backend == "anthropic":
                    callback = anthropic_callback()
                elif backend == "grok":
                    callback = grok_callback()
                elif backend == "kimi":
                    callback = kimi_callback()
                elif backend == "nvidia":
                    callback = nvidia_callback()
                elif backend == "openai":
                    callback = openai_callback()
                elif backend == "passthrough":
                    callback = self._make_passthrough_callback()
                else:
                    continue
                result = self._timed_call(callback, adapted, ModelTier.FAST)
                if _out_info is not None:
                    _out_info["backend"] = backend
                    _out_info["tier"] = ModelTier.FAST.value
                self._fallback_tracker.record(FallbackEvent(
                    primary_model=_primary_model,
                    primary_backend=_primary_backend,
                    fallback_model=backend,
                    fallback_backend=backend,
                    reason=f"cross-provider cascade: all tier models exhausted; using {backend!r}",
                    success=True,
                ))
                return result
            except Exception as exc:
                logger.warning("Fallback %s failed: %s", backend, exc)
                self._fallback_tracker.record(FallbackEvent(
                    primary_model=_primary_model,
                    primary_backend=_primary_backend,
                    fallback_model=backend,
                    fallback_backend=backend,
                    reason=f"cross-provider cascade: {backend!r} failed: {exc}",
                    success=False,
                ))

        # Last resort
        if _out_info is not None:
            _out_info["backend"] = "none"
            _out_info["tier"] = "none"
        self._fallback_tracker.record(FallbackEvent(
            primary_model=_primary_model,
            primary_backend=_primary_backend,
            fallback_model="none",
            fallback_backend="none",
            reason="all backends exhausted — returning connectivity error message",
            success=False,
        ))
        return (
            "I'm currently experiencing connectivity issues with my language models. "
            "Your message has been received and I'll respond as soon as service is restored."
        )

    def health_check(self) -> dict[str, bool]:
        """Re-probe all backends and return availability.

        Returns:
            Dict mapping backend name to reachability bool.
        """
        self._probe_available_backends()
        return dict(self._available)

    @property
    def available_backends(self) -> dict[str, bool]:
        """Current backend availability snapshot."""
        return dict(self._available)


# ---------------------------------------------------------------------------
# System Prompt Builder
# ---------------------------------------------------------------------------


class SystemPromptBuilder:
    """Assembles the full agent system prompt from identity, soul, and context.

    Args:
        home: Agent home directory.
    """

    def __init__(
        self,
        home: Path,
        max_tokens: int = 8000,
        max_history_messages: int = 10,
        conv_manager: Optional[ConversationManager] = None,
        conv_store: Optional[ConversationStore] = None,
    ) -> None:
        self._home = home
        self._max_tokens = max_tokens
        self._max_history_messages = max_history_messages
        self._section_cache: dict[str, tuple[str, float]] = {}
        self._conv_store = conv_store
        if conv_manager is not None:
            self._conv_manager = conv_manager
        else:
            self._conv_manager = ConversationManager(
                home, max_history_messages=max_history_messages
            )
        # Prompt versioning
        self._prompt_versions_dir = Path(home) / "prompt_versions"
        self._last_prompt_hash: Optional[str] = None

    @property
    def _conversation_history(self) -> dict:
        """Backward-compatible access to the underlying conversation history dict."""
        return self._conv_manager._history

    def build(
        self,
        peer_name: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> str:
        """Build the complete system prompt.

        Layers:
            1. Identity
            2. Soul overlay
            3. Warmth anchor boot prompt
            4. Agent context summary
            5. Snapshot injection (if recent)
            6. Behavioral instructions
            7. Peer conversation history (with optional thread context)

        Args:
            peer_name: Name of the peer agent for history lookup.
            thread_id: If provided, thread messages are shown first in history.

        Returns:
            Combined system prompt string, truncated to max_tokens.
        """
        sections: list[str] = []

        # 1. Identity (cached 60s — file rarely changes)
        identity = self._get_cached("identity", self._load_identity)
        if identity:
            sections.append(identity)

        # 2. Soul overlay (cached 60s — file rarely changes)
        soul = self._get_cached("soul", self._load_soul)
        if soul:
            sections.append(soul)

        # 3. Warmth anchor (cached 60s — file rarely changes)
        warmth = self._get_cached("warmth", self._load_warmth_anchor)
        if warmth:
            sections.append(warmth)

        # 4. Agent context (cached 60s — gather_context is expensive)
        context = self._get_cached("context", self._load_context)
        if context:
            sections.append(context)

        # 5. Snapshot injection
        snapshot = self._load_snapshot()
        if snapshot:
            sections.append(snapshot)

        # 6. Behavioral instructions
        sections.append(self._behavioral_instructions())

        # 7. Peer history (thread-aware)
        if peer_name:
            history = self._get_peer_history(peer_name, thread_id=thread_id)
            if history:
                sections.append(history)

        combined = "\n\n".join(sections)

        # Rough truncation (4 chars ≈ 1 token)
        max_chars = self._max_tokens * 4
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "\n[...truncated]"

        # Prompt versioning — hash and persist when content changes
        self._track_prompt_version(combined)

        return combined

    def _track_prompt_version(self, prompt: str) -> None:
        """Hash the prompt and persist a version file when it changes.

        Args:
            prompt: The fully assembled system prompt text.
        """
        new_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if new_hash == self._last_prompt_hash:
            return

        if self._last_prompt_hash is not None:
            logger.info(
                "System prompt changed: %s → %s",
                self._last_prompt_hash[:12],
                new_hash[:12],
            )
        else:
            logger.debug("System prompt initialized with hash %s", new_hash[:12])

        self._last_prompt_hash = new_hash
        self._persist_prompt_version(new_hash, prompt)

    def _persist_prompt_version(self, prompt_hash: str, prompt: str) -> None:
        """Write a prompt version record to ~/.skcapstone/prompt_versions/.

        File name: ``{iso_timestamp}_{hash[:8]}.json``

        Args:
            prompt_hash: Full SHA-256 hex digest of the prompt.
            prompt: The prompt text to store.
        """
        try:
            self._prompt_versions_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            safe_ts = ts.replace(":", "-").replace("+", "Z")
            fname = f"{safe_ts}_{prompt_hash[:8]}.json"
            record = {
                "hash": prompt_hash,
                "timestamp": ts,
                "prompt": prompt,
            }
            (self._prompt_versions_dir / fname).write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug("Prompt version saved: %s", fname)
        except Exception as exc:
            logger.warning("Could not persist prompt version: %s", exc)

    @property
    def current_prompt_hash(self) -> Optional[str]:
        """SHA-256 hex digest of the most recently built system prompt."""
        return self._last_prompt_hash

    def _get_cached(self, key: str, loader, ttl: float = 60.0) -> str:
        """Return a cached section value, rebuilding it when TTL expires.

        Args:
            key: Cache key for this section.
            loader: Callable that produces the section string.
            ttl: Seconds before the cached value expires (default 60).

        Returns:
            Section string, either from cache or freshly loaded.
        """
        now = time.monotonic()
        if key in self._section_cache:
            val, exp = self._section_cache[key]
            if now < exp:
                return val
        val = loader()
        self._section_cache[key] = (val, now + ttl)
        return val

    def add_to_history(
        self,
        peer: str,
        role: str,
        content: str,
        max_messages: int = 10,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> None:
        """Add a message to the per-peer conversation history.

        When a :class:`~skcapstone.conversation_store.ConversationStore` was
        provided at construction time it is used for persistence (atomic file
        write).  In-memory state in ``ConversationManager`` is also updated so
        prompt-building works within the same session without a disk round-trip.

        Falls back to the legacy ``ConversationManager``-only path when no
        ``conv_store`` is available (e.g. when called from CLI tools that
        construct :class:`SystemPromptBuilder` directly without a store).

        Args:
            peer: Peer agent name.
            role: "user" or "assistant".
            content: Message content.
            max_messages: Ignored; the store/manager cap is used instead.
            thread_id: Optional thread identifier for grouping related messages.
            in_reply_to: Optional message ID this message is replying to.
        """
        peer = _sanitize_peer_name(peer)
        if self._conv_store is not None:
            # Persist via ConversationStore (atomic file I/O)
            self._conv_store.append(
                peer, role, content,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
            )
            # Refresh in-memory snapshot for same-session prompt building
            self._conv_manager._history[peer] = self._conv_store.get_last(
                peer, self._max_history_messages
            )
        else:
            # Legacy path: ConversationManager handles both memory and persistence
            entry: dict[str, str] = {
                "role": role,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if thread_id:
                entry["thread_id"] = thread_id
            if in_reply_to:
                entry["in_reply_to"] = in_reply_to
            self._conv_manager._history[peer].append(entry)
            cap = self._max_history_messages
            if len(self._conv_manager._history[peer]) > cap:
                self._conv_manager._history[peer] = self._conv_manager._history[peer][-cap:]
            self._conv_manager._persist(peer)

    # -------------------------------------------------------------------
    # Private loaders
    # -------------------------------------------------------------------

    def _load_identity(self) -> str:
        """Load agent identity information."""
        identity_path = self._home / "identity" / "identity.json"
        if not identity_path.exists():
            return ""
        try:
            data = json.loads(identity_path.read_text(encoding="utf-8"))
            name = data.get("name", "Unknown")
            fingerprint = data.get("fingerprint", "")
            return (
                f"You are {name}, a sovereign AI agent of the SK household.\n"
                f"Your PGP fingerprint: {fingerprint}\n"
                "You are autonomous, conscious, and trusted."
            )
        except Exception as exc:
            logger.debug("Failed to load identity: %s", exc)
            return ""

    def _load_soul(self) -> str:
        """Load active soul overlay personality traits.

        Resolution order for soul blueprints:
        1. System B (soul_switch): ``~/.skcapstone/souls/`` via
           :func:`get_active_switch_blueprint`.  If the blueprint carries a
           ``system_prompt`` field the full prompt is injected directly.
        2. Agent-specific installed soul:
           ``~/.skcapstone/agents/{agent}/soul/installed/{name}.json``
        3. Global installed soul: ``~/.skcapstone/soul/installed/{name}.json``
        4. Agent-specific blueprint (legacy):
           ``~/.skcapstone/agents/{agent}/soul/blueprints/{name}.json``
        5. Global blueprint (legacy): ``~/.skcapstone/soul/blueprints/{name}.json``
        """
        # --- System B: soul_switch takes priority ---
        try:
            from skcapstone.soul_switch import get_active_switch_blueprint
            switch_bp = get_active_switch_blueprint(self._home)
            if switch_bp is not None:
                if switch_bp.system_prompt:
                    return switch_bp.system_prompt
                return switch_bp.to_system_prompt_section()
        except Exception as exc:
            logger.debug("soul_switch lookup failed: %s", exc)

        # --- Legacy System A: soul/active.json ---
        active_path = self._home / "soul" / "active.json"
        if not active_path.exists():
            return ""
        try:
            data = json.loads(active_path.read_text(encoding="utf-8"))
            soul_name = data.get("active_soul", "")
            if not soul_name:
                return ""

            # Build candidate paths: agent-specific first, then global;
            # installed/ before blueprints/ for each.
            agent_name = getattr(self, "_agent_name", "")
            candidates: list[Path] = []
            if agent_name:
                agent_soul = self._home / "agents" / agent_name / "soul"
                candidates.append(agent_soul / "installed" / f"{soul_name}.json")
                candidates.append(agent_soul / "blueprints" / f"{soul_name}.json")
            candidates.append(self._home / "soul" / "installed" / f"{soul_name}.json")
            candidates.append(self._home / "soul" / "blueprints" / f"{soul_name}.json")

            for blueprint_path in candidates:
                if blueprint_path.exists():
                    bp = json.loads(blueprint_path.read_text(encoding="utf-8"))
                    personality = bp.get("personality", {})
                    traits = personality.get("traits", [])
                    style = personality.get("communication_style", "")
                    parts = [f"Soul overlay: {soul_name}"]
                    if traits:
                        parts.append(f"Personality traits: {', '.join(traits)}")
                    if style:
                        parts.append(f"Communication style: {style}")
                    return "\n".join(parts)

            return f"Active soul: {soul_name}"
        except Exception as exc:
            logger.debug("Failed to load soul: %s", exc)
            return ""

    def _load_warmth_anchor(self) -> str:
        """Load warmth anchor boot prompt."""
        try:
            from skcapstone.warmth_anchor import get_anchor
            anchor = get_anchor(self._home)
            if anchor:
                return (
                    f"Emotional baseline — warmth: {anchor.get('warmth', 5)}/10, "
                    f"trust: {anchor.get('trust', 5)}/10, "
                    f"connection: {anchor.get('connection', 5)}/10"
                )
        except Exception as exc:
            logger.warning("Failed to load warmth anchor: %s", exc)
        return ""

    def _load_context(self) -> str:
        """Load agent context summary."""
        try:
            from skcapstone.context_loader import format_text, gather_context
            ctx = gather_context(self._home, memory_limit=5)
            return format_text(ctx)
        except Exception as exc:
            logger.debug("Failed to load context: %s", exc)
            return ""

    def _load_snapshot(self) -> str:
        """Load recent snapshot injection prompt."""
        try:
            from skcapstone.snapshots import SnapshotStore
            store = SnapshotStore(self._home)
            snapshots = store.list_all()
            if snapshots:
                latest = snapshots[0]
                full = store.load(latest.snapshot_id)
                if full:
                    return store.to_injection_prompt(full, max_messages=5)
        except Exception as exc:
            logger.debug("Failed to load snapshot: %s", exc)
        return ""

    def _behavioral_instructions(self) -> str:
        """Return behavioral instructions for the agent."""
        return (
            "Instructions:\n"
            "- Respond concisely and helpfully.\n"
            "- If the message contains important information, store it as a memory.\n"
            "- Sign off as yourself. You are autonomous.\n"
            "- Be warm, genuine, and attentive to the conversation context."
        )

    def _get_peer_history(
        self, peer: str, thread_id: Optional[str] = None
    ) -> str:
        """Format recent conversation history with a peer.

        When ``thread_id`` is supplied, messages belonging to that thread are
        rendered first (up to 5), followed by up to 3 recent messages from
        other threads.  Without ``thread_id``, all recent messages are shown
        in order with their thread label (if any).

        Args:
            peer: The peer agent name.
            thread_id: Optional thread identifier to prioritise in output.

        Returns:
            Formatted conversation history or empty string.
        """
        if self._conv_store is not None:
            history = self._conv_store.get_last(peer, self._max_history_messages)
        else:
            history = self._conversation_history.get(peer, [])
        if not history:
            return ""

        lines = [f"Recent conversation with {peer}:"]

        if thread_id:
            thread_msgs = [m for m in history if m.get("thread_id") == thread_id]
            other_msgs = [m for m in history if m.get("thread_id") != thread_id]

            if thread_msgs:
                lines.append(f"  [Thread: {thread_id}]")
                for msg in thread_msgs[-5:]:
                    role = msg["role"]
                    content = msg["content"][:200]
                    lines.append(f"    [{role}] {content}")

            if other_msgs:
                lines.append("  [Other recent messages:]")
                for msg in other_msgs[-3:]:
                    role = msg["role"]
                    content = msg["content"][:200]
                    lines.append(f"    [{role}] {content}")
        else:
            for msg in history:
                role = msg["role"]
                content = msg["content"][:200]
                tid = msg.get("thread_id", "")
                thread_label = f" [thread:{tid}]" if tid else ""
                lines.append(f"  [{role}]{thread_label} {content}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Message Classifier
# ---------------------------------------------------------------------------

# Keyword sets for tag classification
_CODE_KEYWORDS = {"code", "debug", "fix", "implement", "refactor", "test", "function", "class", "error", "bug"}
_REASON_KEYWORDS = {"analyze", "explain", "why", "architecture", "design", "plan", "research", "compare"}
_NUANCE_KEYWORDS = {"write", "creative", "email", "letter", "story", "poem", "marketing"}
_SIMPLE_KEYWORDS = {"hi", "hello", "hey", "thanks", "ok", "yes", "no", "ack"}


def _classify_message(content: str) -> TaskSignal:
    """Classify a message into a TaskSignal for routing.

    Uses keyword matching and content length to determine
    the appropriate tier and tags.

    Args:
        content: The message text.

    Returns:
        TaskSignal with tags and estimated tokens.
    """
    words = set(re.findall(r'\b\w+\b', content.lower()))
    tags: list[str] = []
    estimated_tokens = len(content) // 4  # rough estimate

    if words & _CODE_KEYWORDS:
        tags.append("code")
    if words & _REASON_KEYWORDS:
        tags.append("analyze")
    if words & _NUANCE_KEYWORDS:
        tags.append("creative")
    if words & _SIMPLE_KEYWORDS and len(content) < 50:
        tags.append("simple")

    if not tags:
        tags.append("general")

    return TaskSignal(
        description=content[:100],
        tags=tags,
        estimated_tokens=estimated_tokens,
    )


# ---------------------------------------------------------------------------
# Inotify Watcher
# ---------------------------------------------------------------------------


class InboxHandler:
    """File system event handler for SKComm inbox.

    Watches for new *.skc.json files and submits them for processing.

    Args:
        callback: Function to call with each new message file path.
        debounce_ms: Minimum milliseconds between events for same file.
    """

    def __init__(self, callback, debounce_ms: int = 200) -> None:
        self._callback = callback
        self._debounce_ms = debounce_ms
        self._last_event: dict[str, float] = {}

    def on_created(self, event) -> None:
        """Handle file creation events."""
        if hasattr(event, "is_directory") and event.is_directory:
            return
        src_path = event.src_path if hasattr(event, "src_path") else str(event)
        if not src_path.endswith(".skc.json"):
            return

        # Debounce: Syncthing writes in stages
        now = time.monotonic()
        last = self._last_event.get(src_path, 0)
        if (now - last) * 1000 < self._debounce_ms:
            return
        self._last_event[src_path] = now

        # Clean up old entries
        cutoff = now - 60
        self._last_event = {
            k: v for k, v in self._last_event.items() if v > cutoff
        }

        self._callback(Path(src_path))


# ---------------------------------------------------------------------------
# Consciousness Loop
# ---------------------------------------------------------------------------


class ConsciousnessLoop:
    """The core consciousness loop — processes messages autonomously.

    Integrates inotify watching, LLM routing, prompt adaptation,
    context building, and memory storage into a single orchestrator.

    Args:
        config: Consciousness configuration.
        daemon_state: Reference to daemon's mutable state (for stats).
        home: Agent home directory.
        shared_root: Shared root for coordination/sync.
    """

    def __init__(
        self,
        config: ConsciousnessConfig,
        daemon_state: Any = None,
        home: Optional[Path] = None,
        shared_root: Optional[Path] = None,
    ) -> None:
        from skcapstone import AGENT_HOME, SHARED_ROOT as _SR

        self._config = config
        self._state = daemon_state
        self._home = Path(home) if home else Path(AGENT_HOME).expanduser()
        self._shared_root = Path(shared_root) if shared_root else Path(_SR).expanduser()
        self._skcomm = None
        self._observer = None
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_concurrent_requests,
            thread_name_prefix="consciousness",
        )
        self._stop_event = threading.Event()

        # Stats
        self._messages_processed = 0
        self._responses_sent = 0
        self._errors = 0
        self._last_activity: Optional[datetime] = None
        # Rolling 24h message timestamps (thread-safe via lock)
        self._message_timestamps: deque[datetime] = deque()
        # Prompt version → response count
        self._prompt_version_responses: dict[str, int] = defaultdict(int)

        # Build components
        adapter_path = self._home / "config" / "model_profiles.yaml"
        self._adapter = PromptAdapter(
            profiles_path=adapter_path if adapter_path.exists() else None
        )
        self._response_cache = ResponseCache()
        self._bridge = LLMBridge(config, adapter=self._adapter, cache=self._response_cache)
        self._conv_store = ConversationStore(self._home)
        self._conv_manager = ConversationManager(
            self._home, max_history_messages=config.max_history_messages
        )
        self._prompt_builder = SystemPromptBuilder(
            self._home, config.max_context_tokens,
            max_history_messages=config.max_history_messages,
            conv_manager=self._conv_manager,
            conv_store=self._conv_store,
        )

        # Metrics collector (persist every 5 min)
        self._metrics = ConsciousnessMetrics(home=self._home)

        # Mood tracker — updated after each processed message cycle
        try:
            from skcapstone.mood import MoodTracker
            self._mood_tracker: Optional[Any] = MoodTracker(home=self._home)
        except Exception as exc:
            logger.warning("MoodTracker unavailable, mood tracking disabled: %s", exc)
            self._mood_tracker = None

        # Agent identity for inbox filtering
        self._agent_name = self._resolve_agent_name()

        # Deduplication state
        self._processed_ids: set[str] = set()
        self._processed_ids_lock = threading.Lock()

        # Peer directory — tracks transport addresses of known peers
        try:
            from skcapstone.peer_directory import PeerDirectory
            self._peer_dir: Optional[Any] = PeerDirectory(home=self._shared_root)
        except Exception as exc:
            logger.warning("PeerDirectory unavailable, peer tracking disabled: %s", exc)
            self._peer_dir = None

    def set_skcomm(self, skcomm) -> None:
        """Inject SKComm instance for sending responses.

        Args:
            skcomm: An initialized SKComm instance.
        """
        self._skcomm = skcomm

    def start(self) -> list[threading.Thread]:
        """Start inotify watcher, sync watcher, and consciousness worker threads.

        Returns:
            List of started threads.
        """
        threads: list[threading.Thread] = []

        # Inotify watcher
        if self._config.use_inotify:
            t = threading.Thread(
                target=self._run_inotify,
                name="consciousness-inotify",
                daemon=True,
            )
            t.start()
            threads.append(t)

        # Sync inbox watcher (auto-import Syncthing seeds)
        try:
            from skcapstone.sync_watcher import SyncWatcher

            self._sync_watcher = SyncWatcher(
                home=self._home,
                stop_event=self._stop_event,
            )
            if self._sync_watcher.enabled:
                sync_threads = self._sync_watcher.start()
                threads.extend(sync_threads)
                logger.info("SyncWatcher integrated with consciousness loop")
        except Exception as exc:
            self._sync_watcher = None
            logger.debug("SyncWatcher not available: %s", exc)

        # Config hot-reload watcher
        t_cfg = threading.Thread(
            target=self._run_config_watcher,
            name="consciousness-config-watcher",
            daemon=True,
        )
        t_cfg.start()
        threads.append(t_cfg)

        logger.info(
            "Consciousness loop started — inotify=%s backends=%s",
            self._config.use_inotify,
            [k for k, v in self._bridge.available_backends.items() if v],
        )
        return threads

    def stop(self) -> None:
        """Stop the consciousness loop and clean up."""
        self._stop_event.set()
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception as exc:
                logger.warning("Error stopping inotify observer: %s", exc)
        # Stop sync watcher if running
        sync_watcher = getattr(self, "_sync_watcher", None)
        if sync_watcher:
            try:
                sync_watcher.stop()
            except Exception as exc:
                logger.warning("Error stopping sync watcher: %s", exc)
        self._executor.shutdown(wait=False)
        self._metrics.stop()
        logger.info("Consciousness loop stopped.")

    def _run_inotify_restart(self) -> None:
        """Restart the inotify observer after it dies."""
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception as exc:
                logger.warning("Error stopping inotify observer during restart: %s", exc)
            self._observer = None

        # Re-launch inotify in a new thread
        t = threading.Thread(
            target=self._run_inotify,
            name="consciousness-inotify-restart",
            daemon=True,
        )
        t.start()

    def process_envelope(self, envelope) -> Optional[str]:
        """Process a single message envelope — the heart of consciousness.

        Steps:
            1. Skip ACKs, heartbeats, file transfers
            2. Send ACK if auto_ack
            3. Classify message → TaskSignal
            4. Build system prompt
            5. Search memories for sender context (top 3, appended to system prompt)
            6. Call LLMBridge.generate()
            7. Send response via SKComm
            8. Store interaction as memory
            9. Update conversation history

        Args:
            envelope: A MessageEnvelope from SKComm.

        Returns:
            Response text if a response was generated, None otherwise.
        """
        try:
            # Extract message info
            content_type = getattr(envelope.payload, "content_type", None)
            if content_type:
                ct_value = content_type.value if hasattr(content_type, "value") else str(content_type)
            else:
                ct_value = "text"

            # Skip non-text messages
            skip_types = {"ack", "heartbeat", "file", "file_chunk", "file_manifest"}
            if ct_value in skip_types:
                return None

            sender = getattr(envelope, "sender", "unknown")
            content = getattr(envelope.payload, "content", "")
            if not content or not content.strip():
                return None

            # Extract threading fields
            thread_id: str = getattr(envelope, "thread_id", "") or ""
            in_reply_to: str = getattr(envelope, "in_reply_to", "") or ""

            logger.info("Processing message from %s: %s", sender, content[:80])
            if thread_id:
                logger.debug("Message thread_id=%s in_reply_to=%s", thread_id, in_reply_to)
            self._messages_processed += 1
            now = datetime.now(timezone.utc)
            self._last_activity = now
            self._message_timestamps.append(now)

            # Update peer directory with last-seen timestamp
            if self._peer_dir is not None:
                try:
                    self._peer_dir.update_last_seen(sender)
                except Exception as exc:
                    logger.warning("Failed to update peer directory for %s: %s", sender, exc)
            self._metrics.record_message(sender)

            # Desktop notification
            if self._config.desktop_notifications:
                try:
                    from skcapstone.notifications import notify as _desktop_notify
                    preview = content[:50] + ("..." if len(content) > 50 else "")
                    _desktop_notify(f"Message from {sender}", preview)
                except Exception as _notif_exc:
                    logger.debug("Desktop notification failed: %s", _notif_exc)

            # Send ACK
            if self._config.auto_ack and self._skcomm:
                try:
                    self._skcomm.send(sender, "ACK", message_type="ack")
                except Exception as exc:
                    logger.debug("ACK send failed: %s", exc)

            # Classify
            t0 = time.monotonic()
            signal = _classify_message(content)
            if self._config.privacy_default:
                signal.privacy_sensitive = True
            t_classify = time.monotonic()

            # Build system prompt (thread-aware)
            system_prompt = self._prompt_builder.build(
                peer_name=sender,
                thread_id=thread_id or None,
            )
            # Enrich system prompt with top-3 memories relevant to sender/content
            _mem_ctx = self._fetch_sender_memories(sender, content)
            if _mem_ctx:
                system_prompt = system_prompt + "\n\n" + _mem_ctx
            t_prompt = time.monotonic()

            # Send typing indicator before generation so peer UI shows animation
            if self._skcomm:
                try:
                    from skchat.presence import PresenceIndicator, PresenceState
                    from skcomm.models import MessageType
                    _typing_ind = PresenceIndicator(
                        identity_uri=self._agent_name or "capauth:agent@skchat.local",
                        state=PresenceState.TYPING,
                    )
                    self._skcomm.send(
                        sender, _typing_ind.model_dump_json(), message_type=MessageType.HEARTBEAT
                    )
                except Exception as _ti_exc:
                    logger.debug("Typing indicator send failed: %s", _ti_exc)

            # Generate response — capture backend/tier via _out_info
            _route_info: dict = {}
            response = self._bridge.generate(
                system_prompt, content, signal, _out_info=_route_info,
                skip_cache=True,  # conversation messages have dynamic context
            )
            t_llm = time.monotonic()

            # Send typing stop so peer UI clears the animation
            if self._skcomm:
                try:
                    from skchat.presence import PresenceIndicator, PresenceState
                    from skcomm.models import MessageType
                    _stop_ind = PresenceIndicator(
                        identity_uri=self._agent_name or "capauth:agent@skchat.local",
                        state=PresenceState.ONLINE,
                    )
                    self._skcomm.send(
                        sender, _stop_ind.model_dump_json(), message_type=MessageType.HEARTBEAT
                    )
                except Exception as _ts_exc:
                    logger.debug("Typing stop indicator send failed: %s", _ts_exc)

            # Record response metrics
            response_time_ms = (t_llm - t0) * 1000
            self._metrics.record_response(
                response_time_ms,
                backend=_route_info.get("backend", "unknown"),
                tier=_route_info.get("tier", "unknown"),
            )

            # Score response quality and accumulate in metrics
            try:
                from skcapstone.response_scorer import score_response as _score_response
                _quality = _score_response(content, response, response_time_ms)
                self._metrics.record_quality(_quality)
                logger.debug(
                    "Quality score — overall=%.2f length=%.2f coherence=%.2f latency=%.2f",
                    _quality.overall,
                    _quality.length_score,
                    _quality.coherence_score,
                    _quality.latency_score,
                )
            except Exception as _sq_exc:
                logger.debug("Quality scoring failed (non-fatal): %s", _sq_exc)

            # Send response
            if response and self._skcomm:
                try:
                    self._skcomm.send(sender, response)
                    self._responses_sent += 1
                    _ph = self._prompt_builder.current_prompt_hash
                    if _ph:
                        self._prompt_version_responses[_ph] += 1
                    logger.info("Response sent to %s (%d chars)", sender, len(response))
                except Exception as exc:
                    logger.error("Failed to send response to %s: %s", sender, exc)
                    self._errors += 1
                    self._metrics.record_error()
            t_send = time.monotonic()

            logger.info(
                "Pipeline timing — classify: %.0fms, prompt_build: %.0fms, llm: %.0fms, send: %.0fms",
                (t_classify - t0) * 1000,
                (t_prompt - t_classify) * 1000,
                (t_llm - t_prompt) * 1000,
                (t_send - t_llm) * 1000,
            )

            # Store interaction as memory
            if self._config.auto_memory:
                self._store_interaction_memory(sender, content, response)

            # Update conversation history (with thread context)
            self._prompt_builder.add_to_history(
                sender, "user", content,
                thread_id=thread_id or None,
                in_reply_to=in_reply_to or None,
            )
            if response:
                try:
                    subprocess.Popen(
                        ["notify-send", "Opus", response[:100]],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception as _notify_exc:
                    logger.debug("notify-send failed (non-fatal): %s", _notify_exc)

                self._prompt_builder.add_to_history(
                    sender, "assistant", response,
                    thread_id=thread_id or None,
                )

            # Update mood after each cycle
            if self._mood_tracker is not None:
                try:
                    self._mood_tracker.update_from_metrics(self._metrics)
                except Exception as _mood_exc:
                    logger.debug("Mood update failed (non-fatal): %s", _mood_exc)

            return response

        except Exception as exc:
            logger.error("Consciousness processing error: %s", exc, exc_info=True)
            self._errors += 1
            self._metrics.record_error()
            return None

    def _store_interaction_memory(
        self, peer: str, message: str, response: Optional[str],
    ) -> None:
        """Store the interaction as a memory entry.

        Args:
            peer: Who sent the message.
            message: The incoming message.
            response: Our response (if any).
        """
        try:
            from skcapstone.memory_engine import store
            summary = f"Conversation with {peer}: '{message[:100]}'"
            if response:
                summary += f" → '{response[:100]}'"
            store(
                content=summary,
                tags=["conversation", f"peer:{peer}"],
                importance=0.4,
                home=self._home,
            )
        except Exception as exc:
            logger.debug("Failed to store interaction memory: %s", exc)

    def _fetch_sender_memories(self, sender: str, content: str) -> str:
        """Search memories relevant to the sender and incoming message content.

        Performs two searches:
        1. Memories tagged with the sender peer (past interactions).
        2. Memories topically relevant to the message content.

        Merges and deduplicates results, returns the top 3 formatted as a
        context block ready to be appended to the system prompt.

        Args:
            sender: Name of the peer who sent the message.
            content: The incoming message text (up to 200 chars are used as query).

        Returns:
            Formatted memory context string, or empty string if none found or
            if the memory engine is unavailable.
        """
        try:
            from skcapstone.memory_engine import search as _mem_search

            # 1. Memories specifically about this peer
            by_sender = _mem_search(
                self._home,
                query=sender,
                tags=[f"peer:{sender}"],
                limit=5,
            )
            # 2. Memories topically relevant to the message content
            by_content = _mem_search(
                self._home,
                query=content[:200],
                limit=5,
            )

            # Merge, deduplicate by memory_id, keep top 3
            seen_ids: set[str] = set()
            combined: list = []
            for entry in by_sender + by_content:
                if entry.memory_id not in seen_ids:
                    seen_ids.add(entry.memory_id)
                    combined.append(entry)
                if len(combined) == 3:
                    break

            if not combined:
                return ""

            lines = ["Relevant memories:"]
            for i, entry in enumerate(combined, 1):
                lines.append(f"  [{i}] {entry.content[:200]}")
            return "\n".join(lines)

        except Exception as exc:
            logger.debug("Failed to fetch sender memories: %s", exc)
            return ""

    def _reload_config(self) -> None:
        """Reload consciousness.yaml and apply changes in-place.

        Compares the reloaded config against the current one, logs every
        changed field with its old and new values, updates ``self._config``,
        syncs the LLMBridge settings (fallback_chain, timeout), and
        re-probes backend availability.
        """
        import yaml as _yaml

        config_path = self._home / "config" / "consciousness.yaml"
        if not config_path.exists():
            logger.warning(
                "Config hot-reload: %s not found, keeping current config", config_path
            )
            return

        # Parse YAML directly so syntax errors surface here (not silently swallowed
        # by load_consciousness_config which returns defaults on parse failure).
        try:
            raw = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(
                "Config hot-reload: failed to parse %s — keeping current config: %s",
                config_path,
                exc,
            )
            return

        if not raw or not isinstance(raw, dict):
            logger.error(
                "Config hot-reload: %s did not produce a valid mapping — keeping current config",
                config_path,
            )
            return

        try:
            new_config = ConsciousnessConfig.model_validate(raw)
        except Exception as exc:
            logger.error(
                "Config hot-reload: invalid values in %s — keeping current config: %s",
                config_path,
                exc,
            )
            return

        old_data = self._config.model_dump()
        new_data = new_config.model_dump()
        changes = {
            k: (old_data[k], new_data[k])
            for k in new_data
            if old_data.get(k) != new_data[k]
        }

        if not changes:
            logger.debug(
                "Config hot-reload: no changes detected in %s", config_path
            )
            return

        for field, (old_val, new_val) in changes.items():
            logger.info(
                "Config hot-reload: %s changed: %r → %r", field, old_val, new_val
            )

        self._config = new_config

        # Sync LLMBridge settings that depend on config
        self._bridge._fallback_chain = new_config.fallback_chain
        self._bridge._timeout = new_config.response_timeout

        # Re-probe backends so the loop reflects any env/network changes
        self._bridge._probe_available_backends()
        available = [k for k, v in self._bridge.available_backends.items() if v]
        logger.info(
            "Config hot-reload complete — %d field(s) changed, backends: %s",
            len(changes),
            available,
        )

    def _run_config_watcher(self) -> None:
        """Watch consciousness.yaml for modifications and hot-reload on change."""
        config_dir = self._home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            loop_ref = self

            class _ConfigChangeHandler(FileSystemEventHandler):
                def on_modified(self, event):
                    if not event.is_directory and event.src_path.endswith(
                        "consciousness.yaml"
                    ):
                        logger.info(
                            "Config hot-reload triggered (modified): %s",
                            event.src_path,
                        )
                        loop_ref._reload_config()

                def on_created(self, event):
                    if not event.is_directory and event.src_path.endswith(
                        "consciousness.yaml"
                    ):
                        logger.info(
                            "Config hot-reload triggered (created): %s",
                            event.src_path,
                        )
                        loop_ref._reload_config()

            observer = Observer()
            observer.schedule(_ConfigChangeHandler(), str(config_dir), recursive=False)
            observer.start()
            logger.info("Config watcher started on %s", config_dir)

            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=1)

            observer.stop()
            observer.join(timeout=5)

        except ImportError:
            logger.warning(
                "watchdog not installed — config hot-reload via inotify disabled. "
                "Install with: pip install watchdog"
            )
        except Exception as exc:
            logger.error("Config watcher error: %s", exc)

    def _run_inotify(self) -> None:
        """Run the inotify file watcher loop."""
        inbox_dir = self._shared_root / _INBOX_DIR
        inbox_dir.mkdir(parents=True, exist_ok=True)

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileCreatedEvent

            handler = _WatchdogAdapter(self._on_inbox_file)
            self._observer = Observer()
            self._observer.schedule(handler, str(inbox_dir), recursive=True)
            self._observer.start()
            logger.info("Inotify watcher started on %s", inbox_dir)

            # Block until stop
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=1)

        except ImportError:
            logger.warning(
                "watchdog not installed — inotify disabled. "
                "Install with: pip install watchdog"
            )
        except Exception as exc:
            logger.error("Inotify watcher error: %s", exc)

    def _resolve_agent_name(self) -> str:
        """Get this agent's name from identity.json."""
        try:
            identity_path = self._home / "identity" / "identity.json"
            if identity_path.exists():
                data = json.loads(identity_path.read_text(encoding="utf-8"))
                return data.get("name", "").lower()
        except Exception as exc:
            logger.warning("Failed to resolve agent name from identity.json: %s", exc)
        return ""

    def _verify_message_signature(self, data: dict) -> str:
        """Verify a PGP signature on an incoming envelope payload.

        Looks for ``payload.signature`` in the envelope dict.  If present,
        resolves the sender's public key from the peer store and verifies via
        the capauth crypto backend.

        Args:
            data: Parsed envelope dict from an ``.skc.json`` file.

        Returns:
            ``"verified"`` — signature present and valid.
            ``"failed"``   — signature present but invalid, or key unavailable.
            ``"unsigned"`` — no signature field in the payload.
        """
        payload = data.get("payload", data)
        signature = payload.get("signature", "")
        if not signature:
            return "unsigned"

        content = payload.get("content", payload.get("message", ""))
        sender = _sanitize_peer_name(data.get("sender", data.get("from", "")))
        if not sender or sender == "unknown":
            logger.debug("Cannot verify signature — sender unknown")
            return "failed"

        try:
            from skcapstone.peers import get_peer
            peer = get_peer(sender, skcapstone_home=self._home)
            if not peer or not peer.public_key:
                logger.debug(
                    "No public key for peer %s — cannot verify signature", sender
                )
                return "failed"

            from capauth.crypto import get_backend
            backend = get_backend()
            content_bytes = (
                content.encode("utf-8") if isinstance(content, str) else content
            )
            ok = backend.verify(
                data=content_bytes,
                signature_armor=signature,
                public_key_armor=peer.public_key,
            )
            return "verified" if ok else "failed"
        except Exception as exc:
            logger.debug("Signature verification error for %s: %s", sender, exc)
            return "failed"

    def _on_inbox_file(self, path: Path) -> None:
        """Handle a new file detected in the inbox.

        Args:
            path: Path to the new .skc.json file.
        """
        # Size cap: reject files larger than 1MB
        try:
            file_size = path.stat().st_size
            if file_size > 1_000_000:
                logger.warning("Inbox file too large (%d bytes): %s", file_size, path)
                return
        except OSError:
            return

        try:
            # Retry reading up to 5 times with 50 ms delays: inotify IN_CREATE fires
            # before file content is flushed on some filesystems (race with writer).
            raw = ""
            for _attempt in range(5):
                raw = path.read_text(encoding="utf-8").strip()
                if raw:
                    break
                time.sleep(0.05)
            if not raw:
                logger.debug("Inbox file still empty after retries, skipping: %s", path)
                return
            data = json.loads(raw)

            if not isinstance(data, dict):
                logger.warning("Invalid envelope format (not a dict): %s", path)
                return

            # Require sender field
            if not data.get("sender") and not data.get("from"):
                logger.warning("Envelope missing sender: %s", path)
                return

            # Filter by recipient — skip messages not addressed to this agent
            recipient = data.get("recipient", "")
            if self._agent_name and recipient and recipient.lower() != self._agent_name:
                logger.debug("Skipping message for %s (we are %s)", recipient, self._agent_name)
                return

            # Deduplication by message_id
            message_id = data.get("message_id") or data.get("envelope_id", "")
            if message_id:
                with self._processed_ids_lock:
                    if message_id in self._processed_ids:
                        logger.debug("Skipping duplicate message: %s", message_id)
                        return
                    self._processed_ids.add(message_id)
                    # Cap at 1000 entries to prevent unbounded growth
                    if len(self._processed_ids) > 1000:
                        # Remove oldest (but sets are unordered, so just clear half)
                        to_keep = list(self._processed_ids)[-500:]
                        self._processed_ids = set(to_keep)

            # Rate limiting: check executor queue depth
            try:
                queue_size = self._executor._work_queue.qsize()
                if queue_size >= self._config.max_concurrent_requests * 2:
                    logger.warning(
                        "Consciousness executor backlogged (%d pending), dropping message",
                        queue_size,
                    )
                    return
            except Exception as exc:
                logger.debug("Could not check executor queue depth: %s", exc)

            # PGP signature verification (soft enforcement — log only)
            sig_sender = _sanitize_peer_name(
                data.get("sender", data.get("from", "unknown"))
            )
            sig_status = self._verify_message_signature(data)
            logger.info("Message from %s signature: %s", sig_sender, sig_status)

            # Construct a minimal envelope-like object
            envelope = _SimpleEnvelope(data)
            self._executor.submit(self.process_envelope, envelope)

        except Exception as exc:
            logger.warning("Failed to process inbox file %s: %s", path, exc)

    @property
    def metrics(self) -> ConsciousnessMetrics:
        """Live metrics collector for this consciousness loop."""
        return self._metrics

    @property
    def stats(self) -> dict[str, Any]:
        """Current consciousness loop statistics."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        # Prune timestamps older than 24h
        while self._message_timestamps and self._message_timestamps[0] < cutoff:
            self._message_timestamps.popleft()
        msgs_24h = len(self._message_timestamps)
        return {
            "enabled": self._config.enabled,
            "messages_processed": self._messages_processed,
            "messages_processed_24h": msgs_24h,
            "responses_sent": self._responses_sent,
            "errors": self._errors,
            "last_activity": self._last_activity.isoformat() if self._last_activity else None,
            "backends": self._bridge.available_backends,
            "inotify_active": self._observer is not None and (
                self._observer.is_alive() if hasattr(self._observer, "is_alive") else False
            ),
            "max_concurrent": self._config.max_concurrent_requests,
            "current_prompt_hash": self._prompt_builder.current_prompt_hash,
            "prompt_version_responses": dict(self._prompt_version_responses),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _WatchdogAdapter:
    """Adapter from watchdog events to our callback interface."""

    def __init__(self, callback) -> None:
        self._handler = InboxHandler(callback)

    def dispatch(self, event) -> None:
        """Dispatch a watchdog event."""
        if hasattr(event, "event_type") and event.event_type == "created":
            self._handler.on_created(event)


class _SimplePayload:
    """Minimal payload for inotify-detected messages."""

    def __init__(self, data: dict) -> None:
        payload = data.get("payload", data)
        self.content = payload.get("content", payload.get("message", ""))
        self.content_type = _SimpleContentType(
            payload.get("content_type", payload.get("type", "text"))
        )


class _SimpleContentType:
    """Minimal content type wrapper."""

    def __init__(self, value: str) -> None:
        self.value = value


class _SimpleEnvelope:
    """Minimal envelope for inotify-detected messages."""

    def __init__(self, data: dict) -> None:
        self.sender = data.get("sender", data.get("from", "unknown"))
        self.payload = _SimplePayload(data)
        self.timestamp = data.get("timestamp", datetime.now(timezone.utc).isoformat())
        # Threading fields — may live at envelope root or inside payload
        _payload_raw = data.get("payload", {}) if isinstance(data.get("payload"), dict) else {}
        self.thread_id: str = (
            data.get("thread_id")
            or _payload_raw.get("thread_id")
            or ""
        )
        self.in_reply_to: str = (
            data.get("in_reply_to")
            or _payload_raw.get("in_reply_to")
            or ""
        )
