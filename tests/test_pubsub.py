"""Tests for sovereign pub/sub messaging."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.pubsub import (
    PubSub,
    Subscription,
    TopicMessage,
    _sanitize_topic,
    _unsanitize_topic,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home."""
    return tmp_path


@pytest.fixture
def bus(home: Path) -> PubSub:
    """Create an initialized PubSub instance."""
    ps = PubSub(home, agent_name="opus")
    ps.initialize()
    return ps


# ---------------------------------------------------------------------------
# Topic name sanitization
# ---------------------------------------------------------------------------


class TestSanitization:
    """Tests for topic name conversion."""

    def test_sanitize_dots(self) -> None:
        """Dots are preserved (used as separators)."""
        assert _sanitize_topic("system.health") == "system.health"

    def test_sanitize_slashes(self) -> None:
        """Slashes become double dashes."""
        assert _sanitize_topic("team/dev") == "team--dev"

    def test_unsanitize_roundtrip(self) -> None:
        """Sanitize then unsanitize returns original."""
        original = "team/dev"
        assert _unsanitize_topic(_sanitize_topic(original)) == original


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    """Tests for PubSub setup."""

    def test_initialize_creates_dirs(self, home: Path) -> None:
        """Initialize creates the directory structure."""
        PubSub(home).initialize()
        assert (home / "pubsub").is_dir()
        assert (home / "pubsub" / "topics").is_dir()
        assert (home / "pubsub" / "dead-letter").is_dir()

    def test_initialize_idempotent(self, bus: PubSub, home: Path) -> None:
        """Multiple initializations don't break anything."""
        bus.initialize()
        bus.initialize()
        assert (home / "pubsub" / "topics").is_dir()


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


class TestPublish:
    """Tests for message publishing."""

    def test_publish_creates_topic_dir(self, bus: PubSub, home: Path) -> None:
        """Publishing creates the topic directory."""
        bus.publish("system.health", {"status": "alive"})
        assert (home / "pubsub" / "topics" / "system.health").is_dir()

    def test_publish_writes_message_file(self, bus: PubSub, home: Path) -> None:
        """Publishing writes a JSON message file."""
        msg = bus.publish("test.topic", {"key": "value"})
        topic_dir = home / "pubsub" / "topics" / "test.topic"
        files = list(topic_dir.glob("msg-*.json"))
        assert len(files) == 1

    def test_publish_returns_message(self, bus: PubSub) -> None:
        """Publish returns a complete TopicMessage."""
        msg = bus.publish("t", {"data": 42})
        assert msg.topic == "t"
        assert msg.sender == "opus"
        assert msg.payload == {"data": 42}
        assert msg.message_id

    def test_publish_multiple_messages(self, bus: PubSub, home: Path) -> None:
        """Multiple publishes to same topic create separate files."""
        bus.publish("multi", {"n": 1})
        bus.publish("multi", {"n": 2})
        bus.publish("multi", {"n": 3})
        files = list((home / "pubsub" / "topics" / "multi").glob("msg-*.json"))
        assert len(files) == 3

    def test_publish_with_tags(self, bus: PubSub) -> None:
        """Messages can have tags."""
        msg = bus.publish("tagged", {"x": 1}, tags=["critical", "health"])
        assert msg.tags == ["critical", "health"]

    def test_publish_with_custom_ttl(self, bus: PubSub) -> None:
        """Custom TTL is set on the message."""
        msg = bus.publish("short-lived", {}, ttl_seconds=60)
        assert msg.ttl_seconds == 60

    def test_prune_excess_messages(self, home: Path) -> None:
        """Topic is pruned when exceeding max messages."""
        bus = PubSub(home, agent_name="opus", max_topic_messages=3)
        bus.initialize()
        for i in range(5):
            bus.publish("pruned", {"n": i})
        files = list((home / "pubsub" / "topics" / "pruned").glob("msg-*.json"))
        assert len(files) == 3


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


class TestSubscribe:
    """Tests for subscription management."""

    def test_subscribe_creates_entry(self, bus: PubSub) -> None:
        """Subscribe creates a subscription record."""
        sub = bus.subscribe("system.*")
        assert sub.pattern == "system.*"

    def test_subscribe_idempotent(self, bus: PubSub) -> None:
        """Subscribing twice returns existing subscription."""
        s1 = bus.subscribe("test.topic")
        s2 = bus.subscribe("test.topic")
        assert s1.subscribed_at == s2.subscribed_at

    def test_subscribe_persists(self, bus: PubSub, home: Path) -> None:
        """Subscriptions are persisted to disk."""
        bus.subscribe("persistent")
        subs_file = home / "pubsub" / "subscriptions.json"
        assert subs_file.exists()
        data = json.loads(subs_file.read_text(encoding="utf-8"))
        assert "persistent" in data

    def test_unsubscribe(self, bus: PubSub) -> None:
        """Unsubscribe removes the subscription."""
        bus.subscribe("temporary")
        assert bus.unsubscribe("temporary") is True
        subs = bus.list_subscriptions()
        assert "temporary" not in subs

    def test_unsubscribe_nonexistent(self, bus: PubSub) -> None:
        """Unsubscribing from a nonexistent pattern returns False."""
        assert bus.unsubscribe("ghost") is False

    def test_list_subscriptions(self, bus: PubSub) -> None:
        """List all active subscriptions."""
        bus.subscribe("a.*")
        bus.subscribe("b.topic")
        subs = bus.list_subscriptions()
        assert len(subs) == 2


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


class TestPoll:
    """Tests for message polling."""

    def test_poll_specific_topic(self, bus: PubSub) -> None:
        """Poll a specific topic returns its messages."""
        bus.publish("poll.test", {"n": 1})
        bus.publish("poll.test", {"n": 2})
        msgs = bus.poll(topic="poll.test")
        assert len(msgs) == 2

    def test_poll_subscribed_topics(self, bus: PubSub) -> None:
        """Poll with no topic returns all subscribed messages."""
        bus.subscribe("sub.*")
        bus.publish("sub.a", {"n": 1})
        bus.publish("sub.b", {"n": 2})
        bus.publish("other", {"n": 3})  # not subscribed
        msgs = bus.poll()
        assert len(msgs) == 2

    def test_poll_with_since_filter(self, bus: PubSub) -> None:
        """Since filter excludes older messages."""
        bus.publish("time.test", {"n": 1})
        cutoff = datetime.now(timezone.utc)
        bus.publish("time.test", {"n": 2})
        msgs = bus.poll(topic="time.test", since=cutoff)
        assert len(msgs) == 1
        assert msgs[0].payload["n"] == 2

    def test_poll_limit(self, bus: PubSub) -> None:
        """Limit caps the number of returned messages."""
        for i in range(10):
            bus.publish("many", {"n": i})
        msgs = bus.poll(topic="many", limit=3)
        assert len(msgs) == 3

    def test_poll_skips_expired(self, bus: PubSub, home: Path) -> None:
        """Expired messages are not returned."""
        msg = bus.publish("expiry", {"data": "old"}, ttl_seconds=1)
        # Manually backdate the message
        topic_dir = home / "pubsub" / "topics" / "expiry"
        msg_file = list(topic_dir.glob("msg-*.json"))[0]
        data = json.loads(msg_file.read_text(encoding="utf-8"))
        data["published_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        msg_file.write_text(json.dumps(data), encoding="utf-8")

        msgs = bus.poll(topic="expiry")
        assert len(msgs) == 0

    def test_poll_wildcard_subscription(self, bus: PubSub) -> None:
        """Wildcard subscriptions match multiple topics."""
        bus.subscribe("team.*")
        bus.publish("team.dev", {"n": 1})
        bus.publish("team.ops", {"n": 2})
        bus.publish("system.health", {"n": 3})
        msgs = bus.poll()
        assert len(msgs) == 2

    def test_poll_empty_topic(self, bus: PubSub) -> None:
        """Polling a topic with no messages returns empty list."""
        msgs = bus.poll(topic="empty.topic")
        assert msgs == []


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


class TestCallbacks:
    """Tests for callback-based message dispatch."""

    def test_on_message_registers_callback(self, bus: PubSub) -> None:
        """Registering a callback also subscribes."""
        received: list[TopicMessage] = []
        bus.on_message("cb.test", lambda msg: received.append(msg))
        subs = bus.list_subscriptions()
        assert "cb.test" in subs

    def test_poll_and_dispatch(self, bus: PubSub) -> None:
        """Dispatch triggers callbacks for matching messages."""
        received: list[TopicMessage] = []
        bus.on_message("dispatch.*", lambda msg: received.append(msg))
        bus.publish("dispatch.a", {"n": 1})
        bus.publish("dispatch.b", {"n": 2})
        count = bus.poll_and_dispatch()
        assert count == 2
        assert len(received) == 2

    def test_callback_error_doesnt_stop_dispatch(self, bus: PubSub) -> None:
        """A failing callback doesn't prevent others from running."""
        results: list[int] = []

        def failing_cb(msg: TopicMessage) -> None:
            raise RuntimeError("boom")

        def good_cb(msg: TopicMessage) -> None:
            results.append(1)

        bus.on_message("error.test", failing_cb)
        bus.on_message("error.test", good_cb)
        bus.publish("error.test", {"x": 1})
        bus.poll_and_dispatch()
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Topic listing and status
# ---------------------------------------------------------------------------


class TestListAndStatus:
    """Tests for topic listing and status."""

    def test_list_topics(self, bus: PubSub) -> None:
        """List all topics with message counts."""
        bus.publish("topic.a", {"n": 1})
        bus.publish("topic.a", {"n": 2})
        bus.publish("topic.b", {"n": 1})
        topics = bus.list_topics()
        assert len(topics) == 2
        topic_a = next(t for t in topics if t["topic"] == "topic.a")
        assert topic_a["messages"] == 2

    def test_status_summary(self, bus: PubSub) -> None:
        """Status returns structured summary."""
        bus.subscribe("s.*")
        bus.publish("s.a", {"n": 1})
        status = bus.status()
        assert status["agent"] == "opus"
        assert status["subscriptions"] == 1
        assert status["topics"] >= 1
        assert status["total_messages"] >= 1


# ---------------------------------------------------------------------------
# Expiry purge
# ---------------------------------------------------------------------------


class TestPurge:
    """Tests for expired message cleanup."""

    def test_purge_removes_expired(self, bus: PubSub, home: Path) -> None:
        """Purge removes expired messages."""
        bus.publish("purge.test", {"data": "old"}, ttl_seconds=1)
        # Backdate the message
        topic_dir = home / "pubsub" / "topics" / "purge.test"
        msg_file = list(topic_dir.glob("msg-*.json"))[0]
        data = json.loads(msg_file.read_text(encoding="utf-8"))
        data["published_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        msg_file.write_text(json.dumps(data), encoding="utf-8")

        removed = bus.purge_expired()
        assert removed == 1
        assert len(list(topic_dir.glob("msg-*.json"))) == 0

    def test_purge_keeps_valid(self, bus: PubSub) -> None:
        """Purge doesn't remove valid messages."""
        bus.publish("keep.test", {"data": "fresh"}, ttl_seconds=86400)
        removed = bus.purge_expired()
        assert removed == 0


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for Pydantic models."""

    def test_topic_message_defaults(self) -> None:
        """TopicMessage has sensible defaults."""
        msg = TopicMessage(topic="t", sender="a")
        assert msg.ttl_seconds == 86400
        assert msg.tags == []
        assert not msg.is_expired

    def test_expired_message(self) -> None:
        """Expired message is detected."""
        msg = TopicMessage(
            topic="t",
            sender="a",
            published_at=datetime.now(timezone.utc) - timedelta(hours=25),
            ttl_seconds=86400,
        )
        assert msg.is_expired

    def test_subscription_defaults(self) -> None:
        """Subscription has sensible defaults."""
        sub = Subscription(pattern="test.*")
        assert sub.last_read is None
        assert sub.message_count == 0
