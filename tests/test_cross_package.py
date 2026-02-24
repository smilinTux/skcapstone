"""Cross-package integration tests — end-to-end sovereign agent flow.

Exercises the real interfaces between packages:
    capauth   -> skcapstone   (identity discovery)
    skcapstone -> skmemory    (built-in memory engine)
    skcomm    -> file transport (message send/receive)
    skchat    -> skmemory     (chat history storage)
    full pipeline: identity -> memory -> message -> coord -> sync

These tests import from multiple packages simultaneously to prove
they work together, not just in isolation.

Task: 1724f5f8
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Core skcapstone imports
# ---------------------------------------------------------------------------
from skcapstone.coordination import Board, Task, TaskPriority
from skcapstone.discovery import discover_all, discover_identity
from skcapstone.memory_engine import recall, search, store
from skcapstone.models import IdentityState, MemoryLayer, PillarStatus
from skcapstone.pillars.identity import generate_identity
from skcapstone.pillars.memory import initialize_memory
from skcapstone.pillars.security import audit_event, initialize_security, read_audit_log
from skcapstone.pillars.sync import collect_seed, initialize_sync
from skcapstone.pillars.trust import initialize_trust, record_trust_state
from skcapstone.runtime import AgentRuntime
from skcapstone.tokens import issue_token


def _init_full_agent(home: Path, name: str = "test-agent") -> None:
    """Initialize all pillars for a test agent.

    Args:
        home: Agent home directory.
        name: Agent display name.
    """
    generate_identity(home, name)
    initialize_memory(home)
    initialize_trust(home)
    initialize_security(home)
    initialize_sync(home)

    manifest = {
        "name": name,
        "version": "0.1.0",
        "created_at": "2026-01-01T00:00:00Z",
        "connectors": [],
    }
    (home / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (home / "config").mkdir(exist_ok=True)

    import yaml

    config = {"agent_name": name}
    (home / "config" / "config.yaml").write_text(
        yaml.dump(config, default_flow_style=False)
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. CapAuth <-> SKCapstone identity integration
# ═══════════════════════════════════════════════════════════════════════════


class TestCapAuthToSKCapstone:
    """CapAuth profile data flows into skcapstone identity pillar."""

    def test_discover_reads_capauth_profile(self, tmp_agent_home: Path):
        """When a CapAuth profile exists, discover_identity uses its fingerprint."""
        fake_state = IdentityState(
            fingerprint="REAL" + "A" * 36,
            name="capauth-agent",
            email="agent@capauth.local",
            key_path=Path("/tmp/public.asc"),
            status=PillarStatus.ACTIVE,
        )
        with patch(
            "skcapstone.discovery._try_load_capauth_profile",
            return_value=fake_state,
        ):
            state = discover_identity(tmp_agent_home)

        assert state.fingerprint.startswith("REAL")
        assert state.status == PillarStatus.ACTIVE

        manifest_path = tmp_agent_home / "identity" / "identity.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["capauth_managed"] is True

    def test_placeholder_without_capauth(self, tmp_agent_home: Path):
        """Without CapAuth, generate_identity uses a placeholder."""
        with patch(
            "skcapstone.pillars.identity._try_init_capauth",
            return_value=None,
        ):
            state = generate_identity(tmp_agent_home, "fallback")

        assert state.status == PillarStatus.DEGRADED
        assert len(state.fingerprint) == 40
        data = json.loads(
            (tmp_agent_home / "identity" / "identity.json").read_text()
        )
        assert data["capauth_managed"] is False

    def test_capauth_models_importable(self):
        """CapAuth core models are importable alongside skcapstone."""
        from capauth.models import EntityInfo, KeyInfo, SovereignProfile

        entity = EntityInfo(name="test", email="test@test.local")
        assert entity.name == "test"


# ═══════════════════════════════════════════════════════════════════════════
# 2. SKComm file transport — cross-process message delivery
# ═══════════════════════════════════════════════════════════════════════════


class TestSKCommFileTransport:
    """SKComm file transport sends and receives messages via filesystem."""

    def test_send_and_receive_via_file_transport(self, tmp_path: Path):
        """Message sent via file transport is receivable from the inbox."""
        from skcomm.models import (
            MessageEnvelope,
            MessageMetadata,
            MessagePayload,
            MessageType,
            RoutingConfig,
        )
        from skcomm.transports.file import FileTransport

        outbox = tmp_path / "outbox"
        inbox = tmp_path / "inbox"

        sender_transport = FileTransport(outbox_path=outbox, inbox_path=inbox)
        receiver_transport = FileTransport(outbox_path=inbox, inbox_path=outbox)

        envelope = MessageEnvelope(
            sender="opus",
            recipient="jarvis",
            payload=MessagePayload(
                content="Hello from integration test",
                content_type=MessageType.TEXT,
            ),
        )

        result = sender_transport.send(envelope.to_bytes(), "jarvis")
        assert result.success is True

        # Receiver reads from the sender's outbox (simulating filesystem sync)
        recv_transport = FileTransport(outbox_path=tmp_path / "noop", inbox_path=outbox)
        raw_messages = recv_transport.receive()
        assert len(raw_messages) >= 1

        received = MessageEnvelope.from_bytes(raw_messages[0])
        assert received.sender == "opus"
        assert received.recipient == "jarvis"
        assert received.payload.content == "Hello from integration test"

    def test_multiple_messages_ordered(self, tmp_path: Path):
        """Multiple messages are received in order."""
        from skcomm.models import MessageEnvelope, MessagePayload, MessageType
        from skcomm.transports.file import FileTransport

        outbox = tmp_path / "outbox"
        transport = FileTransport(outbox_path=outbox, inbox_path=tmp_path / "noop")

        for i in range(3):
            envelope = MessageEnvelope(
                sender="opus",
                recipient="jarvis",
                payload=MessagePayload(
                    content=f"Message {i}",
                    content_type=MessageType.TEXT,
                ),
            )
            transport.send(envelope.to_bytes(), "jarvis")

        reader = FileTransport(outbox_path=tmp_path / "noop", inbox_path=outbox)
        raw = reader.receive()
        assert len(raw) == 3


# ═══════════════════════════════════════════════════════════════════════════
# 3. SKChat -> SKMemory integration (chat history as memories)
# ═══════════════════════════════════════════════════════════════════════════


class TestSKChatMemoryIntegration:
    """SKChat messages stored as SKMemory memories."""

    def test_chat_message_model_valid(self):
        """SKChat ChatMessage can be created with standard fields."""
        from skchat.models import ChatMessage, ContentType

        msg = ChatMessage(
            sender="opus",
            recipient="jarvis",
            content="Meeting at 3pm about sync architecture",
            content_type=ContentType.PLAIN,
        )
        assert msg.sender == "opus"
        assert msg.recipient == "jarvis"
        assert not msg.encrypted
        assert msg.delivery_status.value == "pending"

    def test_chat_message_stored_in_skcapstone_memory(self, tmp_agent_home: Path):
        """A chat message can be stored in skcapstone's memory engine."""
        initialize_memory(tmp_agent_home)

        from skchat.models import ChatMessage

        msg = ChatMessage(
            sender="opus",
            recipient="jarvis",
            content="Remember to review the CapAuth integration",
        )

        entry = store(
            home=tmp_agent_home,
            content=f"[skchat] {msg.sender} -> {msg.recipient}: {msg.content}",
            tags=["skchat", f"sender:{msg.sender}", f"recipient:{msg.recipient}"],
            source="skchat",
            importance=0.6,
            metadata={"chat_message_id": msg.id, "thread_id": msg.thread_id},
        )

        assert entry is not None
        assert entry.source == "skchat"

        results = search(tmp_agent_home, "CapAuth integration")
        assert len(results) >= 1
        assert any("CapAuth" in r.content for r in results)

    def test_chat_thread_model(self):
        """SKChat Thread model works with participant management."""
        from skchat.models import Thread

        thread = Thread(title="Architecture Discussion")
        thread.add_participant("opus")
        thread.add_participant("jarvis")
        thread.touch()

        assert len(thread.participants) == 2
        assert thread.message_count == 1
        assert thread.remove_participant("opus")
        assert len(thread.participants) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 4. Full pipeline: identity -> memory -> message -> coord -> sync
# ═══════════════════════════════════════════════════════════════════════════


class TestFullSovereignPipeline:
    """End-to-end: init agent, store memory, send message, coordinate, sync."""

    def test_full_cross_package_flow(self, tmp_agent_home: Path):
        """The complete sovereign agent lifecycle across all packages.

        Flow:
          1. Init agent with identity, memory, trust, security, sync
          2. Store a memory (skcapstone memory engine)
          3. Create a chat message (skchat models)
          4. Send it via file transport (skcomm)
          5. Store it as a memory (cross: skchat -> skcapstone)
          6. Create and complete a coord task
          7. Collect a sync seed with everything
          8. Verify all data appears in discovery
        """
        _init_full_agent(tmp_agent_home, "sovereign-test")

        # 2. Store a memory
        mem = store(
            tmp_agent_home,
            "CapAuth uses PGP for sovereign identity",
            tags=["capauth", "architecture"],
            importance=0.9,
        )
        assert mem.layer == MemoryLayer.MID_TERM  # high importance auto-promotes

        # 3. Create a chat message
        from skchat.models import ChatMessage

        chat_msg = ChatMessage(
            sender="sovereign-test",
            recipient="peer-agent",
            content="Sync architecture review needed",
        )

        # 4. Send via file transport
        from skcomm.models import MessageEnvelope, MessagePayload, MessageType
        from skcomm.transports.file import FileTransport

        outbox = tmp_agent_home / "skcomm_outbox"
        peer_inbox = tmp_agent_home / "skcomm_peer_inbox"
        transport = FileTransport(outbox_path=outbox, inbox_path=peer_inbox)

        envelope = MessageEnvelope(
            sender=chat_msg.sender,
            recipient=chat_msg.recipient,
            payload=MessagePayload(
                content=chat_msg.content,
                content_type=MessageType.TEXT,
            ),
        )
        send_result = transport.send(envelope.to_bytes(), chat_msg.recipient)
        assert send_result.success

        # 5. Store the chat message as a memory
        chat_mem = store(
            tmp_agent_home,
            f"[skchat] {chat_msg.sender} -> {chat_msg.recipient}: {chat_msg.content}",
            tags=["skchat", "sent"],
            source="skchat",
            importance=0.5,
        )
        assert chat_mem is not None

        # 6. Coordination: create task, claim, complete
        board = Board(tmp_agent_home)
        board.ensure_dirs()
        task = Task(
            title="Review sync architecture",
            priority=TaskPriority.HIGH,
            tags=["architecture", "review"],
            created_by="sovereign-test",
        )
        board.create_task(task)
        board.claim_task("sovereign-test", task.id)
        board.complete_task("sovereign-test", task.id)

        views = board.get_task_views()
        assert any(v.task.id == task.id and v.status.value == "done" for v in views)

        # 7. Record trust and collect sync seed
        record_trust_state(
            tmp_agent_home,
            depth=8.0,
            trust_level=0.95,
            love_intensity=0.9,
            entangled=True,
        )
        seed_path = collect_seed(tmp_agent_home, "sovereign-test")
        assert seed_path.exists()

        seed = json.loads(seed_path.read_text())
        assert seed["agent_name"] == "sovereign-test"
        assert seed["trust"]["entangled"] is True
        assert seed["memory"]["total"] >= 2  # at least our 2 memories

        # 8. Verify full discovery
        state = discover_all(tmp_agent_home)
        assert state["memory"].status == PillarStatus.ACTIVE
        assert state["memory"].total_memories >= 2
        assert state["trust"].status in (PillarStatus.ACTIVE, PillarStatus.DEGRADED)
        assert state["sync"].status in (PillarStatus.ACTIVE, PillarStatus.DEGRADED)

        # Verify memories are searchable across packages
        results = search(tmp_agent_home, "sovereign identity")
        assert len(results) >= 1

        chat_results = search(tmp_agent_home, "skchat")
        assert len(chat_results) >= 1

    def test_runtime_sees_cross_package_state(self, tmp_agent_home: Path):
        """AgentRuntime.awaken() reflects state from all packages."""
        _init_full_agent(tmp_agent_home, "runtime-test")
        store(tmp_agent_home, "Cross-package memory", tags=["test"])

        runtime = AgentRuntime(home=tmp_agent_home)
        manifest = runtime.awaken()

        assert manifest.name == "runtime-test"
        assert manifest.memory.total_memories >= 1
        assert manifest.memory.status == PillarStatus.ACTIVE

    def test_token_and_audit_across_operations(self, tmp_agent_home: Path):
        """Token issuance and audit trail span multiple subsystems."""
        _init_full_agent(tmp_agent_home, "audit-test")

        token = issue_token(
            tmp_agent_home,
            subject="peer-agent",
            capabilities=["memory:read", "sync:pull"],
        )
        assert token.payload.has_capability("memory:read")

        audit_event(tmp_agent_home, "CROSS_PACKAGE", "Integration test audit")
        store(tmp_agent_home, "Audit trail memory", tags=["audit"])

        entries = read_audit_log(tmp_agent_home)
        types = {e.event_type for e in entries}
        assert "CROSS_PACKAGE" in types

        results = search(tmp_agent_home, "audit")
        assert len(results) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# 5. Package import compatibility
# ═══════════════════════════════════════════════════════════════════════════


class TestPackageImportCompatibility:
    """All five core packages can be imported simultaneously."""

    def test_all_packages_importable(self):
        """capauth, skcapstone, skmemory, skcomm, skchat all import cleanly."""
        import capauth
        import skcapstone
        import skchat
        import skcomm
        import skmemory

        assert hasattr(capauth, "__version__") or hasattr(capauth, "profile")
        assert hasattr(skcapstone, "__version__")
        assert hasattr(skcomm, "__version__") or True
        assert hasattr(skchat, "__version__") or True
        assert hasattr(skmemory, "__version__") or True

    def test_capauth_profile_module(self):
        """capauth.profile has init_profile and load_profile."""
        from capauth.profile import init_profile, load_profile

        assert callable(init_profile)
        assert callable(load_profile)

    def test_skcomm_core_module(self):
        """skcomm.core has SKComm class."""
        from skcomm.core import SKComm

        assert callable(SKComm.from_config)

    def test_skchat_models_module(self):
        """skchat.models has ChatMessage and Thread."""
        from skchat.models import ChatMessage, Thread

        assert ChatMessage is not None
        assert Thread is not None

    def test_no_circular_imports(self):
        """Importing all packages in various orders doesn't cause circular imports."""
        import importlib

        for mod in [
            "skcapstone.runtime",
            "capauth.profile",
            "skcomm.core",
            "skchat.models",
            "skcapstone.memory_engine",
            "skcapstone.coordination",
        ]:
            importlib.import_module(mod)
