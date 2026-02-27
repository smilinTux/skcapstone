"""
Sovereign pub/sub — lightweight real-time messaging for agent meshes.

Topic-based publish/subscribe built on the file transport layer.
Designed for 100+ node scale without requiring a central broker.
Each agent manages its own subscriptions and topic inboxes.

Architecture:
    Publishers write topic messages to a shared topic directory.
    Subscribers poll their subscribed topics or register callbacks.
    Syncthing distributes topic directories across the mesh.

Storage layout:
    ~/.skcapstone/pubsub/
    ├── subscriptions.json     # Agent's active subscriptions
    ├── topics/                # Topic message directories
    │   ├── system.health/     # Topic: system.health
    │   │   ├── msg-<uuid>.json
    │   │   └── ...
    │   └── team.dev/          # Topic: team.dev
    │       └── ...
    └── dead-letter/           # Undeliverable messages

Usage:
    bus = PubSub(home, agent_name="opus")
    bus.subscribe("system.health")
    bus.subscribe("team.*")                # wildcard
    bus.publish("system.health", {"status": "alive", "load": 0.4})
    messages = bus.poll("system.health", since=last_check)
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.pubsub")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TopicMessage(BaseModel):
    """A single published message on a topic."""

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    topic: str
    sender: str
    payload: dict[str, Any] = Field(default_factory=dict)
    published_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_seconds: int = Field(default=86400, description="Message expiry (default 24h)")
    tags: list[str] = Field(default_factory=list)

    @property
    def is_expired(self) -> bool:
        """Check if this message has expired."""
        expires = self.published_at + timedelta(seconds=self.ttl_seconds)
        return datetime.now(timezone.utc) > expires


class Subscription(BaseModel):
    """An agent's subscription to a topic pattern."""

    pattern: str = Field(description="Topic name or glob pattern (e.g., 'team.*')")
    subscribed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_read: Optional[datetime] = None
    message_count: int = 0


# ---------------------------------------------------------------------------
# PubSub
# ---------------------------------------------------------------------------

class PubSub:
    """Sovereign publish/subscribe message bus.

    File-based, mesh-friendly, zero-broker architecture.
    Each agent runs its own PubSub instance that reads from
    shared topic directories (distributed via Syncthing).

    Args:
        home: Agent home directory (~/.skcapstone).
        agent_name: Name of the local agent.
        max_topic_messages: Maximum messages per topic before pruning.
    """

    def __init__(
        self,
        home: Path,
        agent_name: str = "anonymous",
        max_topic_messages: int = 1000,
    ) -> None:
        self._home = home
        self._agent = agent_name
        self._max_messages = max_topic_messages
        self._pubsub_dir = home / "pubsub"
        self._topics_dir = self._pubsub_dir / "topics"
        self._dead_letter_dir = self._pubsub_dir / "dead-letter"
        self._subs_file = self._pubsub_dir / "subscriptions.json"
        self._callbacks: dict[str, list[Callable]] = {}

    def initialize(self) -> None:
        """Create the pub/sub directory structure."""
        self._pubsub_dir.mkdir(parents=True, exist_ok=True)
        self._topics_dir.mkdir(exist_ok=True)
        self._dead_letter_dir.mkdir(exist_ok=True)

    def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        ttl_seconds: int = 86400,
        tags: Optional[list[str]] = None,
    ) -> TopicMessage:
        """Publish a message to a topic.

        Creates the topic directory if it doesn't exist and writes
        the message as a JSON file. Prunes old messages if the topic
        exceeds max_topic_messages.

        Args:
            topic: Topic name (e.g., 'system.health', 'team.dev').
            payload: Message payload dict.
            ttl_seconds: Message time-to-live in seconds.
            tags: Optional tags for filtering.

        Returns:
            The published TopicMessage.
        """
        self.initialize()

        msg = TopicMessage(
            topic=topic,
            sender=self._agent,
            payload=payload,
            ttl_seconds=ttl_seconds,
            tags=tags or [],
        )

        topic_dir = self._topics_dir / _sanitize_topic(topic)
        topic_dir.mkdir(parents=True, exist_ok=True)

        filename = f"msg-{msg.message_id}.json"
        tmp_path = topic_dir / f".{filename}.tmp"
        final_path = topic_dir / filename

        tmp_path.write_text(
            msg.model_dump_json(indent=2),
            encoding="utf-8",
        )
        tmp_path.rename(final_path)

        self._prune_topic(topic_dir)

        logger.debug("Published to '%s': %s", topic, msg.message_id)
        return msg

    def subscribe(self, pattern: str) -> Subscription:
        """Subscribe to a topic or topic pattern.

        Supports glob patterns (e.g., 'team.*', 'system.health',
        '*.critical'). The subscription is persisted to disk.

        Args:
            pattern: Topic name or glob pattern.

        Returns:
            The new or existing Subscription.
        """
        self.initialize()
        subs = self._load_subscriptions()

        existing = subs.get(pattern)
        if existing:
            return existing

        sub = Subscription(pattern=pattern)
        subs[pattern] = sub
        self._save_subscriptions(subs)

        logger.info("Agent '%s' subscribed to '%s'", self._agent, pattern)
        return sub

    def unsubscribe(self, pattern: str) -> bool:
        """Remove a subscription.

        Args:
            pattern: The pattern to unsubscribe from.

        Returns:
            True if the subscription existed and was removed.
        """
        subs = self._load_subscriptions()
        if pattern not in subs:
            return False

        del subs[pattern]
        self._save_subscriptions(subs)
        self._callbacks.pop(pattern, None)
        logger.info("Agent '%s' unsubscribed from '%s'", self._agent, pattern)
        return True

    def poll(
        self,
        topic: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[TopicMessage]:
        """Poll for new messages on subscribed topics.

        Args:
            topic: Specific topic to poll (None = all subscribed).
            since: Only return messages after this timestamp.
            limit: Maximum messages to return.

        Returns:
            List of TopicMessage objects, newest first.
        """
        self.initialize()

        if topic:
            topics = [topic]
        else:
            subs = self._load_subscriptions()
            topics = self._resolve_subscribed_topics(subs)

        messages: list[TopicMessage] = []
        for t in topics:
            topic_dir = self._topics_dir / _sanitize_topic(t)
            if not topic_dir.is_dir():
                continue
            for msg_file in sorted(topic_dir.glob("msg-*.json")):
                try:
                    data = json.loads(msg_file.read_text(encoding="utf-8"))
                    msg = TopicMessage.model_validate(data)
                    if msg.is_expired:
                        continue
                    if since and msg.published_at <= since:
                        continue
                    messages.append(msg)
                except (json.JSONDecodeError, Exception) as exc:
                    logger.warning("Skipping invalid message %s: %s", msg_file.name, exc)

        messages.sort(key=lambda m: m.published_at, reverse=True)

        if topic:
            subs = self._load_subscriptions()
            for pattern, sub in subs.items():
                if fnmatch.fnmatch(topic, pattern):
                    sub.last_read = datetime.now(timezone.utc)
                    sub.message_count += len(messages[:limit])
            self._save_subscriptions(subs)

        return messages[:limit]

    def on_message(self, pattern: str, callback: Callable[[TopicMessage], None]) -> None:
        """Register a callback for messages matching a pattern.

        Callbacks are triggered during poll_and_dispatch().

        Args:
            pattern: Topic pattern to match.
            callback: Function called with each matching TopicMessage.
        """
        if pattern not in self._callbacks:
            self._callbacks[pattern] = []
        self._callbacks[pattern].append(callback)
        self.subscribe(pattern)

    def poll_and_dispatch(self, since: Optional[datetime] = None) -> int:
        """Poll all subscriptions and dispatch to registered callbacks.

        Args:
            since: Only process messages after this timestamp.

        Returns:
            Number of messages dispatched.
        """
        dispatched = 0
        messages = self.poll(since=since)

        for msg in messages:
            for pattern, callbacks in self._callbacks.items():
                if fnmatch.fnmatch(msg.topic, pattern):
                    for cb in callbacks:
                        try:
                            cb(msg)
                            dispatched += 1
                        except Exception as exc:
                            logger.error(
                                "Callback error for '%s' on '%s': %s",
                                pattern, msg.topic, exc,
                            )

        return dispatched

    def list_topics(self) -> list[dict[str, Any]]:
        """List all known topics with message counts.

        Returns:
            List of dicts with topic name, message count, and latest timestamp.
        """
        self.initialize()
        topics: list[dict[str, Any]] = []

        if not self._topics_dir.is_dir():
            return topics

        for topic_dir in sorted(self._topics_dir.iterdir()):
            if not topic_dir.is_dir():
                continue
            msg_files = list(topic_dir.glob("msg-*.json"))
            latest = None
            if msg_files:
                try:
                    newest = max(msg_files, key=lambda f: f.stat().st_mtime)
                    data = json.loads(newest.read_text(encoding="utf-8"))
                    latest = data.get("published_at")
                except (json.JSONDecodeError, OSError):
                    pass

            topics.append({
                "topic": _unsanitize_topic(topic_dir.name),
                "messages": len(msg_files),
                "latest": latest,
            })

        return topics

    def list_subscriptions(self) -> dict[str, Subscription]:
        """Return the agent's current subscriptions."""
        return self._load_subscriptions()

    def purge_expired(self) -> int:
        """Remove expired messages from all topics.

        Returns:
            Number of expired messages removed.
        """
        removed = 0
        if not self._topics_dir.is_dir():
            return removed

        for topic_dir in self._topics_dir.iterdir():
            if not topic_dir.is_dir():
                continue
            for msg_file in topic_dir.glob("msg-*.json"):
                try:
                    data = json.loads(msg_file.read_text(encoding="utf-8"))
                    msg = TopicMessage.model_validate(data)
                    if msg.is_expired:
                        msg_file.unlink()
                        removed += 1
                except (json.JSONDecodeError, Exception):
                    pass

        if removed:
            logger.info("Purged %d expired messages", removed)
        return removed

    def status(self) -> dict[str, Any]:
        """Return pub/sub status summary."""
        subs = self._load_subscriptions()
        topics = self.list_topics()
        total_messages = sum(t["messages"] for t in topics)

        return {
            "agent": self._agent,
            "subscriptions": len(subs),
            "topics": len(topics),
            "total_messages": total_messages,
            "callbacks_registered": sum(len(cbs) for cbs in self._callbacks.values()),
            "pubsub_dir": str(self._pubsub_dir),
        }

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _load_subscriptions(self) -> dict[str, Subscription]:
        """Load subscriptions from disk."""
        if not self._subs_file.exists():
            return {}
        try:
            data = json.loads(self._subs_file.read_text(encoding="utf-8"))
            return {k: Subscription.model_validate(v) for k, v in data.items()}
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Failed to load subscriptions: %s", exc)
            return {}

    def _save_subscriptions(self, subs: dict[str, Subscription]) -> None:
        """Persist subscriptions to disk."""
        self._pubsub_dir.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump(mode="json") for k, v in subs.items()}
        self._subs_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _resolve_subscribed_topics(self, subs: dict[str, Subscription]) -> list[str]:
        """Resolve subscription patterns to actual topic directories."""
        if not self._topics_dir.is_dir():
            return []

        all_topics = [
            _unsanitize_topic(d.name)
            for d in self._topics_dir.iterdir()
            if d.is_dir()
        ]

        matched: set[str] = set()
        for pattern in subs:
            for topic in all_topics:
                if fnmatch.fnmatch(topic, pattern):
                    matched.add(topic)

        return sorted(matched)

    def _prune_topic(self, topic_dir: Path) -> None:
        """Remove oldest messages if topic exceeds max size."""
        msg_files = sorted(topic_dir.glob("msg-*.json"), key=lambda f: f.stat().st_mtime)
        excess = len(msg_files) - self._max_messages
        if excess > 0:
            for f in msg_files[:excess]:
                f.unlink()
            logger.debug("Pruned %d old messages from %s", excess, topic_dir.name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_topic(topic: str) -> str:
    """Convert topic name to filesystem-safe directory name."""
    return topic.replace("/", "--").replace(" ", "_")


def _unsanitize_topic(dirname: str) -> str:
    """Reverse of _sanitize_topic."""
    return dirname.replace("--", "/").replace("_", " ")
