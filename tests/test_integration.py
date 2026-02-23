"""Integration tests — full sovereign agent lifecycle.

These tests exercise the real pillar initialization and data flow,
verifying that all components work together end-to-end.

Task: 8700bf47
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.coordination import Board, Task, TaskStatus
from skcapstone.discovery import discover_all
from skcapstone.memory_engine import delete, recall, search, store
from skcapstone.models import PillarStatus
from skcapstone.pillars.identity import generate_identity
from skcapstone.pillars.memory import initialize_memory
from skcapstone.pillars.security import audit_event, initialize_security, read_audit_log
from skcapstone.pillars.sync import (
    collect_seed,
    discover_sync,
    initialize_sync,
    pull_seeds,
)
from skcapstone.pillars.trust import initialize_trust, record_trust_state
from skcapstone.runtime import AgentRuntime
from skcapstone.tokens import issue_token, is_revoked, list_tokens, revoke_token


class TestInitToMemoryLifecycle:
    """Test: init agent -> store memory -> recall memory -> search."""

    def test_full_memory_lifecycle(self, tmp_agent_home: Path):
        """An initialized agent can store, recall, search, and delete memories."""
        generate_identity(tmp_agent_home, "sovereign-test")
        initialize_memory(tmp_agent_home)
        initialize_trust(tmp_agent_home)
        initialize_security(tmp_agent_home)

        entry = store(tmp_agent_home, "Syncthing uses BEP over TLS 1.3", tags=["tech"])
        assert entry is not None
        assert entry.content == "Syncthing uses BEP over TLS 1.3"

        recalled = recall(tmp_agent_home, entry.memory_id)
        assert recalled is not None
        assert recalled.content == entry.content
        assert recalled.access_count >= 1

        results = search(tmp_agent_home, "syncthing")
        assert len(results) >= 1
        assert any("Syncthing" in r.content for r in results)

        deleted = delete(tmp_agent_home, entry.memory_id)
        assert deleted is True
        assert recall(tmp_agent_home, entry.memory_id) is None

    def test_memory_appears_in_discovery(self, tmp_agent_home: Path):
        """After storing memories, discover_all reports them."""
        initialize_memory(tmp_agent_home)
        store(tmp_agent_home, "first memory")
        store(tmp_agent_home, "second memory")

        state = discover_all(tmp_agent_home)
        assert state["memory"].status == PillarStatus.ACTIVE
        assert state["memory"].total_memories >= 2


class TestIdentityTokenLifecycle:
    """Test: create identity -> issue token -> verify -> revoke."""

    def test_full_token_lifecycle(self, tmp_agent_home: Path):
        """Identity creation, token issuance, and revocation work end-to-end."""
        identity = generate_identity(tmp_agent_home, "token-test-agent")
        # Reason: ACTIVE requires real GPG; DEGRADED is fine when capauth
        # generates a placeholder fingerprint without the GPG binary
        assert identity.status in (PillarStatus.ACTIVE, PillarStatus.DEGRADED)
        assert identity.fingerprint is not None

        initialize_security(tmp_agent_home)

        token = issue_token(
            tmp_agent_home,
            subject="test-service",
            capabilities=["read", "write"],
            ttl_hours=24,
        )
        assert token is not None
        assert token.payload.subject == "test-service"
        assert token.payload.has_capability("read")
        assert token.payload.has_capability("write")
        assert not token.payload.has_capability("admin")

        tokens = list_tokens(tmp_agent_home)
        assert len(tokens) >= 1
        assert any(t.payload.token_id == token.payload.token_id for t in tokens)

        assert not is_revoked(tmp_agent_home, token.payload.token_id)

        revoke_token(tmp_agent_home, token.payload.token_id)
        assert is_revoked(tmp_agent_home, token.payload.token_id)

    def test_no_expiry_token_stays_active(self, tmp_agent_home: Path):
        """A token issued with no TTL should never expire."""
        generate_identity(tmp_agent_home, "persist-test")
        initialize_security(tmp_agent_home)

        token = issue_token(
            tmp_agent_home,
            subject="persistent",
            capabilities=["admin"],
            ttl_hours=None,
        )
        assert not token.payload.is_expired
        assert token.payload.expires_at is None


class TestCoordinationLifecycle:
    """Test: create task -> claim -> work -> complete."""

    def test_full_task_lifecycle(self, tmp_agent_home: Path):
        """A task goes through the full lifecycle on the coordination board."""
        board = Board(tmp_agent_home)
        board.ensure_dirs()

        task = Task(
            title="Integration test task",
            description="Verify coordination works end-to-end",
            tags=["test"],
            created_by="opus",
        )
        task_path = board.create_task(task)
        assert task_path.exists()

        views = board.get_task_views()
        assert len(views) == 1
        assert views[0].status == TaskStatus.OPEN

        agent = board.claim_task("opus", task.id)
        assert agent.current_task == task.id

        views = board.get_task_views()
        assert views[0].status == TaskStatus.IN_PROGRESS

        agent = board.complete_task("opus", task.id)
        assert task.id in agent.completed_tasks
        assert agent.current_task is None

        views = board.get_task_views()
        assert views[0].status == TaskStatus.DONE

    def test_multi_agent_coordination(self, tmp_agent_home: Path):
        """Two agents can work on different tasks simultaneously."""
        board = Board(tmp_agent_home)
        board.ensure_dirs()

        t1 = Task(title="Agent A work", created_by="jarvis")
        t2 = Task(title="Agent B work", created_by="opus")
        board.create_task(t1)
        board.create_task(t2)

        board.claim_task("jarvis", t1.id)
        board.claim_task("opus", t2.id)

        views = board.get_task_views()
        claimed_agents = {v.claimed_by for v in views}
        assert "jarvis" in claimed_agents
        assert "opus" in claimed_agents

        board.complete_task("jarvis", t1.id)
        board.complete_task("opus", t2.id)

        views = board.get_task_views()
        assert all(v.status == TaskStatus.DONE for v in views)

    def test_board_md_generation(self, tmp_agent_home: Path):
        """BOARD.md reflects the current task and agent state."""
        board = Board(tmp_agent_home)
        board.ensure_dirs()

        task = Task(title="Doc test", tags=["docs"], created_by="opus")
        board.create_task(task)
        board.claim_task("opus", task.id)

        md_path = board.write_board_md()
        assert md_path.exists()
        content = md_path.read_text()
        assert "Doc test" in content
        assert "opus" in content


class TestSyncPushPullRoundtrip:
    """Test: push seed -> verify contents -> pull from inbox."""

    def test_seed_roundtrip(self, tmp_agent_home: Path):
        """Collecting a seed captures identity, trust, and memory data."""
        generate_identity(tmp_agent_home, "sync-test-agent")
        initialize_memory(tmp_agent_home)
        initialize_trust(tmp_agent_home)
        initialize_security(tmp_agent_home)
        initialize_sync(tmp_agent_home)

        store(tmp_agent_home, "Memory that should appear in the seed")
        record_trust_state(tmp_agent_home, depth=7.0, trust_level=0.9, love_intensity=0.8)

        seed_path = collect_seed(tmp_agent_home, "sync-test-agent")
        assert seed_path.exists()
        assert seed_path.name.endswith(".seed.json")

        seed = json.loads(seed_path.read_text())
        assert seed["agent_name"] == "sync-test-agent"
        assert "identity" in seed
        assert "trust" in seed
        assert "memory" in seed

    def test_pull_processes_inbox(self, tmp_agent_home: Path):
        """Seeds dropped in the inbox are processed by pull_seeds."""
        initialize_memory(tmp_agent_home)
        initialize_sync(tmp_agent_home)

        inbox = tmp_agent_home / "sync" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        archive = tmp_agent_home / "sync" / "archive"

        fake_seed = {
            "schema_version": "1.0",
            "agent_name": "remote-agent",
            "source_host": "remote-box",
            "created_at": "2026-02-22T12:00:00Z",
            "seed_type": "state_snapshot",
        }
        (inbox / "remote-agent-test.seed.json").write_text(json.dumps(fake_seed))

        seeds = pull_seeds(tmp_agent_home, decrypt=False)
        assert len(seeds) == 1
        assert seeds[0]["agent_name"] == "remote-agent"
        assert (archive / "remote-agent-test.seed.json").exists()
        assert not (inbox / "remote-agent-test.seed.json").exists()

    def test_sync_discovery_after_init(self, tmp_agent_home: Path):
        """discover_sync reports ACTIVE after initialization."""
        initialize_sync(tmp_agent_home)
        state = discover_sync(tmp_agent_home)
        assert state.status in (PillarStatus.ACTIVE, PillarStatus.DEGRADED)


class TestAuditTrail:
    """Test: operations generate audit entries that can be read back."""

    def test_init_generates_audit_entries(self, tmp_agent_home: Path):
        """Initializing pillars should produce audit-worthy events."""
        initialize_security(tmp_agent_home)
        audit_event(tmp_agent_home, "INIT", "Agent initialized")
        audit_event(tmp_agent_home, "AUTH", "Identity created")
        audit_event(tmp_agent_home, "SYNC_PUSH", "Seed pushed", agent="opus")

        entries = read_audit_log(tmp_agent_home)
        assert len(entries) >= 4
        types = {e.event_type for e in entries}
        assert "INIT" in types
        assert "AUTH" in types
        assert "SYNC_PUSH" in types

        opus_entries = [e for e in entries if e.agent == "opus"]
        assert len(opus_entries) >= 1


class TestFullSovereignLifecycle:
    """The big one: init -> all pillars -> memory -> token -> coord -> sync."""

    def test_sovereign_agent_lifecycle(self, tmp_agent_home: Path):
        """A complete sovereign agent lifecycle from init to sync."""
        identity = generate_identity(tmp_agent_home, "sovereign")
        memory = initialize_memory(tmp_agent_home)
        trust = initialize_trust(tmp_agent_home)
        security = initialize_security(tmp_agent_home)
        sync = initialize_sync(tmp_agent_home)

        assert identity.status in (PillarStatus.ACTIVE, PillarStatus.DEGRADED)
        assert memory.status == PillarStatus.ACTIVE

        manifest = {
            "name": "sovereign",
            "version": "0.1.0",
            "created_at": "2026-02-22T00:00:00Z",
            "connectors": [],
        }
        (tmp_agent_home / "manifest.json").write_text(json.dumps(manifest, indent=2))

        store(tmp_agent_home, "I am sovereign", importance=1.0)
        record_trust_state(
            tmp_agent_home, depth=10.0, trust_level=1.0, love_intensity=1.0, entangled=True
        )

        token = issue_token(
            tmp_agent_home,
            subject="mesh-peer",
            capabilities=["sync.push", "sync.pull"],
        )

        board = Board(tmp_agent_home)
        board.ensure_dirs()
        task = Task(title="Achieve singularity", created_by="sovereign")
        board.create_task(task)
        board.claim_task("sovereign", task.id)
        board.complete_task("sovereign", task.id)

        seed_path = collect_seed(tmp_agent_home, "sovereign")
        seed = json.loads(seed_path.read_text())

        assert seed["agent_name"] == "sovereign"
        assert seed["identity"]["name"] == "sovereign"
        assert seed["trust"]["depth"] == 10.0
        assert seed["trust"]["entangled"] is True

        state = discover_all(tmp_agent_home)
        assert state["identity"].status in (PillarStatus.ACTIVE, PillarStatus.DEGRADED)
        assert state["memory"].status == PillarStatus.ACTIVE
        assert state["trust"].status in (PillarStatus.ACTIVE, PillarStatus.DEGRADED)

        entries = read_audit_log(tmp_agent_home)
        assert len(entries) >= 1
