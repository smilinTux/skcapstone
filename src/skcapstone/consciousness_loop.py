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

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from skcapstone.blueprints.schema import ModelTier
from skcapstone.model_router import ModelRouter, ModelRouterConfig, RouteDecision, TaskSignal
from skcapstone.prompt_adapter import AdaptedPrompt, PromptAdapter

logger = logging.getLogger("skcapstone.consciousness")

# Default inbox path under shared root
_INBOX_DIR = "sync/comms/inbox"


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
    """

    def __init__(
        self,
        config: ConsciousnessConfig,
        router_config: Optional[ModelRouterConfig] = None,
        adapter: Optional[PromptAdapter] = None,
    ) -> None:
        self._router = ModelRouter(config=router_config)
        self._adapter = adapter or PromptAdapter()
        self._fallback_chain = config.fallback_chain
        self._timeout = config.response_timeout
        self._available: dict[str, bool] = {}
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
        """Check if Ollama is reachable."""
        import urllib.request
        import urllib.error

        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        try:
            req = urllib.request.Request(f"{host}/api/tags")
            with urllib.request.urlopen(req, timeout=2):
                return True
        except Exception:
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
    ) -> str:
        """Route via ModelRouter, adapt prompt, call LLM, cascade on failure.

        Args:
            system_prompt: The agent's system context.
            user_message: The incoming message to respond to.
            signal: Task classification signal.

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

        # Try primary model
        try:
            callback = self._resolve_callback(decision.tier, decision.model_name)
            return self._timed_call(callback, adapted, decision.tier)
        except Exception as exc:
            logger.warning(
                "Primary model %s failed: %s", decision.model_name, exc
            )

        # Try alternate models in same tier
        tier_models = self._router.config.tier_models.get(decision.tier.value, [])
        for alt_model in tier_models[1:]:
            try:
                logger.info("Trying alt model: %s", alt_model)
                alt_adapted = self._adapter.adapt(
                    system_prompt, user_message, alt_model, decision.tier,
                )
                callback = self._resolve_callback(decision.tier, alt_model)
                return self._timed_call(callback, alt_adapted, decision.tier)
            except Exception as exc:
                logger.warning("Alt model %s failed: %s", alt_model, exc)

        # Tier downgrade: try FAST tier
        if decision.tier != ModelTier.FAST:
            fast_models = self._router.config.tier_models.get(ModelTier.FAST.value, [])
            for fast_model in fast_models:
                try:
                    logger.info("Downgrading to FAST tier: %s", fast_model)
                    fast_adapted = self._adapter.adapt(
                        system_prompt, user_message, fast_model, ModelTier.FAST,
                    )
                    callback = self._resolve_callback(ModelTier.FAST, fast_model)
                    return self._timed_call(callback, fast_adapted, ModelTier.FAST)
                except Exception as exc:
                    logger.warning("FAST model %s failed: %s", fast_model, exc)

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
                return self._timed_call(callback, adapted, ModelTier.FAST)
            except Exception as exc:
                logger.warning("Fallback %s failed: %s", backend, exc)

        # Last resort
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

    def __init__(self, home: Path, max_tokens: int = 8000, max_history_messages: int = 10) -> None:
        self._home = home
        self._max_tokens = max_tokens
        self._max_history_messages = max_history_messages
        self._conversations_dir = home / "conversations"
        self._conversation_history: dict[str, list[dict[str, str]]] = defaultdict(list)
        self._load_conversation_files()

    def build(self, peer_name: Optional[str] = None) -> str:
        """Build the complete system prompt.

        Layers:
            1. Identity
            2. Soul overlay
            3. Warmth anchor boot prompt
            4. Agent context summary
            5. Snapshot injection (if recent)
            6. Behavioral instructions
            7. Peer conversation history

        Args:
            peer_name: Name of the peer agent for history lookup.

        Returns:
            Combined system prompt string, truncated to max_tokens.
        """
        sections: list[str] = []

        # 1. Identity
        identity = self._load_identity()
        if identity:
            sections.append(identity)

        # 2. Soul overlay
        soul = self._load_soul()
        if soul:
            sections.append(soul)

        # 3. Warmth anchor
        warmth = self._load_warmth_anchor()
        if warmth:
            sections.append(warmth)

        # 4. Agent context
        context = self._load_context()
        if context:
            sections.append(context)

        # 5. Snapshot injection
        snapshot = self._load_snapshot()
        if snapshot:
            sections.append(snapshot)

        # 6. Behavioral instructions
        sections.append(self._behavioral_instructions())

        # 7. Peer history
        if peer_name:
            history = self._get_peer_history(peer_name)
            if history:
                sections.append(history)

        combined = "\n\n".join(sections)

        # Rough truncation (4 chars ≈ 1 token)
        max_chars = self._max_tokens * 4
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "\n[...truncated]"

        return combined

    def add_to_history(
        self, peer: str, role: str, content: str, max_messages: int = 10,
    ) -> None:
        """Add a message to the per-peer conversation history.

        Appends to the in-memory history, caps it, then atomically persists
        to {home}/conversations/{peer}.json.

        Args:
            peer: Peer agent name.
            role: "user" or "assistant".
            content: Message content.
            max_messages: Max messages to retain per peer.
        """
        self._conversation_history[peer].append({
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        cap = self._max_history_messages
        if len(self._conversation_history[peer]) > cap:
            self._conversation_history[peer] = self._conversation_history[peer][-cap:]
        self._persist_peer_history(peer)

    def _load_conversation_files(self) -> None:
        """Load all existing per-peer conversation files from {home}/conversations/*.json."""
        if not self._conversations_dir.exists():
            return
        for conv_file in self._conversations_dir.glob("*.json"):
            peer = conv_file.stem
            try:
                data = json.loads(conv_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._conversation_history[peer] = data[-self._max_history_messages:]
            except Exception as exc:
                logger.debug("Failed to load conversation file %s: %s", conv_file, exc)

    def _persist_peer_history(self, peer: str) -> None:
        """Atomically write per-peer history to {home}/conversations/{peer}.json.

        Uses a temp file + rename for atomic update.

        Args:
            peer: Peer agent name.
        """
        try:
            self._conversations_dir.mkdir(parents=True, exist_ok=True)
            target = self._conversations_dir / f"{peer}.json"
            tmp = target.with_suffix(".json.tmp")
            payload = json.dumps(self._conversation_history[peer], ensure_ascii=False, indent=2)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(target)
        except Exception as exc:
            logger.debug("Failed to persist conversation for %s: %s", peer, exc)

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
        """Load active soul overlay personality traits."""
        active_path = self._home / "soul" / "active.json"
        if not active_path.exists():
            return ""
        try:
            data = json.loads(active_path.read_text(encoding="utf-8"))
            soul_name = data.get("active_soul", "")
            if not soul_name:
                return ""

            # Try to load the soul blueprint
            blueprint_path = self._home / "soul" / "blueprints" / f"{soul_name}.json"
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
        except Exception:
            pass
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

    def _get_peer_history(self, peer: str) -> str:
        """Format recent conversation history with a peer.

        Args:
            peer: The peer agent name.

        Returns:
            Formatted conversation history or empty string.
        """
        history = self._conversation_history.get(peer, [])
        if not history:
            return ""

        lines = [f"Recent conversation with {peer}:"]
        for msg in history:
            role = msg["role"]
            content = msg["content"][:200]
            lines.append(f"  [{role}] {content}")
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

        # Build components
        adapter_path = self._home / "config" / "model_profiles.yaml"
        self._adapter = PromptAdapter(
            profiles_path=adapter_path if adapter_path.exists() else None
        )
        self._bridge = LLMBridge(config, adapter=self._adapter)
        self._prompt_builder = SystemPromptBuilder(self._home, config.max_context_tokens)

    def set_skcomm(self, skcomm) -> None:
        """Inject SKComm instance for sending responses.

        Args:
            skcomm: An initialized SKComm instance.
        """
        self._skcomm = skcomm

    def start(self) -> list[threading.Thread]:
        """Start inotify watcher and consciousness worker threads.

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
            except Exception:
                pass
        self._executor.shutdown(wait=False)
        logger.info("Consciousness loop stopped.")

    def process_envelope(self, envelope) -> Optional[str]:
        """Process a single message envelope — the heart of consciousness.

        Steps:
            1. Skip ACKs, heartbeats, file transfers
            2. Send ACK if auto_ack
            3. Classify message → TaskSignal
            4. Build system prompt
            5. Call LLMBridge.generate()
            6. Send response via SKComm
            7. Store interaction as memory
            8. Update conversation history

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

            logger.info("Processing message from %s: %s", sender, content[:80])
            self._messages_processed += 1
            self._last_activity = datetime.now(timezone.utc)

            # Send ACK
            if self._config.auto_ack and self._skcomm:
                try:
                    self._skcomm.send(sender, "ACK", content_type="ack")
                except Exception as exc:
                    logger.debug("ACK send failed: %s", exc)

            # Classify
            signal = _classify_message(content)
            if self._config.privacy_default:
                signal.privacy_sensitive = True

            # Build system prompt
            system_prompt = self._prompt_builder.build(peer_name=sender)

            # Generate response
            response = self._bridge.generate(system_prompt, content, signal)

            # Send response
            if response and self._skcomm:
                try:
                    self._skcomm.send(sender, response)
                    self._responses_sent += 1
                    logger.info("Response sent to %s (%d chars)", sender, len(response))
                except Exception as exc:
                    logger.error("Failed to send response to %s: %s", sender, exc)
                    self._errors += 1

            # Store interaction as memory
            if self._config.auto_memory:
                self._store_interaction_memory(sender, content, response)

            # Update conversation history
            self._prompt_builder.add_to_history(sender, "user", content)
            if response:
                self._prompt_builder.add_to_history(sender, "assistant", response)

            return response

        except Exception as exc:
            logger.error("Consciousness processing error: %s", exc, exc_info=True)
            self._errors += 1
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

    def _on_inbox_file(self, path: Path) -> None:
        """Handle a new file detected in the inbox.

        Args:
            path: Path to the new .skc.json file.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))

            # Construct a minimal envelope-like object
            envelope = _SimpleEnvelope(data)
            self._executor.submit(self.process_envelope, envelope)

        except Exception as exc:
            logger.warning("Failed to process inbox file %s: %s", path, exc)

    @property
    def stats(self) -> dict[str, Any]:
        """Current consciousness loop statistics."""
        return {
            "enabled": self._config.enabled,
            "messages_processed": self._messages_processed,
            "responses_sent": self._responses_sent,
            "errors": self._errors,
            "last_activity": self._last_activity.isoformat() if self._last_activity else None,
            "backends": self._bridge.available_backends,
            "inotify_active": self._observer is not None and (
                self._observer.is_alive() if hasattr(self._observer, "is_alive") else False
            ),
            "max_concurrent": self._config.max_concurrent_requests,
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
