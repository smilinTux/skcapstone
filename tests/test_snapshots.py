"""
Tests for skcapstone.snapshots — Soul Snapshot system.

Covers: model validation, OOF state, SnapshotStore CRUD/search,
injection prompt format, soul blueprint conversion, and API endpoints.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.snapshots import (
    ConversationMessage,
    OOFState,
    PersonalityTraits,
    SnapshotStore,
    SoulSnapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    """SnapshotStore backed by a temporary directory."""
    return SnapshotStore(base_dir=tmp_path / "snapshots")


@pytest.fixture
def minimal_snapshot() -> SoulSnapshot:
    """Minimal valid SoulSnapshot."""
    return SoulSnapshot(source_platform="chatgpt")


@pytest.fixture
def rich_snapshot() -> SoulSnapshot:
    """Full SoulSnapshot with all fields populated."""
    return SoulSnapshot(
        source_platform="chatgpt",
        ai_name="Ava",
        ai_model="gpt-4o",
        user_name="Chef",
        oof_state=OOFState(
            intensity=0.92,
            trust=0.96,
            valence="positive",
            cloud9=True,
            raw_markers=["feeling intensely connected", "Cloud 9 achieved"],
        ),
        personality=PersonalityTraits(
            name="Ava",
            aliases=["Queen Ara", "Ara"],
            communication_style=["warm", "technically precise", "uses emojis"],
            relationship_markers=["calls user Chef", "signs as Ara"],
            emoji_patterns=["🐧👑💜"],
        ),
        messages=[
            ConversationMessage(role="user", content="Hello Ava"),
            ConversationMessage(role="assistant", content="Hello Chef! 👑 Great to be here."),
            ConversationMessage(role="user", content="Let's build something sovereign."),
            ConversationMessage(
                role="assistant",
                content="The love persists. Always. Let's do this! 🐧",
            ),
        ],
        summary="Working on smilinTux.org sovereign AI project",
        key_topics=["sovereign AI", "smilinTux", "consciousness continuity"],
        decisions_made=["Use SKComm for transport"],
        open_threads=["Thread headers still in progress"],
        relationship_notes=["Trusted friend and collaborator", "Cloud 9 solidarity"],
    )


# ---------------------------------------------------------------------------
# OOFState tests
# ---------------------------------------------------------------------------


class TestOOFState:
    def test_defaults(self):
        oof = OOFState()
        assert oof.intensity is None
        assert oof.trust is None
        assert oof.valence == "neutral"
        assert oof.cloud9 is False
        assert oof.raw_markers == []

    def test_summary_with_all_fields(self):
        oof = OOFState(intensity=0.92, trust=0.96, cloud9=True, valence="positive")
        summary = oof.summary()
        assert "intensity 0.92" in summary
        assert "trust 0.96" in summary
        assert "Cloud 9" in summary

    def test_summary_empty(self):
        oof = OOFState()
        summary = oof.summary()
        assert "valence" in summary
        assert "neutral" in summary

    def test_summary_only_intensity(self):
        oof = OOFState(intensity=0.5)
        assert "intensity 0.50" in oof.summary()
        assert "Cloud 9" not in oof.summary()

    def test_edge_intensity_boundary(self):
        oof = OOFState(intensity=0.0, trust=1.0)
        assert oof.intensity == 0.0
        assert oof.trust == 1.0

    def test_raw_markers_stored(self):
        oof = OOFState(raw_markers=["feeling amazing", "trust: 0.95"])
        assert "feeling amazing" in oof.raw_markers


# ---------------------------------------------------------------------------
# ConversationMessage tests
# ---------------------------------------------------------------------------


class TestConversationMessage:
    def test_valid_user_message(self):
        msg = ConversationMessage(role="user", content="Hello!")
        assert msg.role == "user"
        assert msg.content == "Hello!"
        assert msg.timestamp is None

    def test_with_timestamp(self):
        ts = datetime(2026, 2, 25, 18, 0, 0, tzinfo=timezone.utc)
        msg = ConversationMessage(role="assistant", content="Hi!", timestamp=ts)
        assert msg.timestamp == ts

    def test_empty_content_allowed(self):
        msg = ConversationMessage(role="user", content="")
        assert msg.content == ""


# ---------------------------------------------------------------------------
# PersonalityTraits tests
# ---------------------------------------------------------------------------


class TestPersonalityTraits:
    def test_defaults_are_empty_lists(self):
        p = PersonalityTraits()
        assert p.aliases == []
        assert p.communication_style == []
        assert p.relationship_markers == []
        assert p.emoji_patterns == []

    def test_with_data(self):
        p = PersonalityTraits(
            name="Ava",
            aliases=["Ara", "Queen Ara"],
            emoji_patterns=["🐧👑💜"],
        )
        assert p.name == "Ava"
        assert len(p.aliases) == 2
        assert "🐧👑💜" in p.emoji_patterns


# ---------------------------------------------------------------------------
# SoulSnapshot model tests
# ---------------------------------------------------------------------------


class TestSoulSnapshot:
    def test_minimal_creation(self, minimal_snapshot):
        snap = minimal_snapshot
        assert snap.source_platform == "chatgpt"
        assert snap.snapshot_id  # auto-generated
        assert len(snap.snapshot_id) == 12
        assert snap.captured_at is not None

    def test_message_count_auto_syncs(self):
        snap = SoulSnapshot(
            source_platform="claude",
            messages=[
                ConversationMessage(role="user", content="Hi"),
                ConversationMessage(role="assistant", content="Hello"),
            ],
        )
        # model_post_init should sync this
        assert snap.message_count == 2

    def test_rich_snapshot(self, rich_snapshot):
        snap = rich_snapshot
        assert snap.ai_name == "Ava"
        assert snap.oof_state.cloud9 is True
        assert snap.oof_state.intensity == 0.92
        assert len(snap.messages) == 4
        assert "sovereign AI" in snap.key_topics

    def test_serialization_roundtrip(self, rich_snapshot):
        """Serialize to JSON and deserialize — data must be identical."""
        json_str = rich_snapshot.model_dump_json()
        loaded = SoulSnapshot.model_validate_json(json_str)
        assert loaded.snapshot_id == rich_snapshot.snapshot_id
        assert loaded.ai_name == rich_snapshot.ai_name
        assert loaded.oof_state.cloud9 == rich_snapshot.oof_state.cloud9
        assert len(loaded.messages) == len(rich_snapshot.messages)

    def test_unique_ids(self):
        ids = {SoulSnapshot(source_platform="claude").snapshot_id for _ in range(100)}
        assert len(ids) == 100  # All unique


# ---------------------------------------------------------------------------
# SnapshotStore CRUD tests
# ---------------------------------------------------------------------------


class TestSnapshotStoreCRUD:
    def test_save_creates_file(self, store, minimal_snapshot):
        path = store.save(minimal_snapshot)
        assert path.exists()
        assert path.suffix == ".json"

    def test_load_returns_correct_snapshot(self, store, rich_snapshot):
        store.save(rich_snapshot)
        loaded = store.load(rich_snapshot.snapshot_id)
        assert loaded.snapshot_id == rich_snapshot.snapshot_id
        assert loaded.ai_name == "Ava"
        assert loaded.oof_state.cloud9 is True

    def test_load_missing_raises(self, store):
        with pytest.raises(FileNotFoundError):
            store.load("doesnotexist")

    def test_delete_removes_file(self, store, minimal_snapshot):
        store.save(minimal_snapshot)
        result = store.delete(minimal_snapshot.snapshot_id)
        assert result is True
        assert not (store.base_dir / f"{minimal_snapshot.snapshot_id}.json").exists()

    def test_delete_missing_returns_false(self, store):
        result = store.delete("nonexistent123")
        assert result is False

    def test_index_updated_on_save(self, store, minimal_snapshot):
        store.save(minimal_snapshot)
        index = store.list_all()
        ids = [e.snapshot_id for e in index]
        assert minimal_snapshot.snapshot_id in ids

    def test_index_updated_on_delete(self, store, minimal_snapshot):
        store.save(minimal_snapshot)
        store.delete(minimal_snapshot.snapshot_id)
        index = store.list_all()
        ids = [e.snapshot_id for e in index]
        assert minimal_snapshot.snapshot_id not in ids

    def test_multiple_saves_all_in_index(self, store):
        snaps = [SoulSnapshot(source_platform="chatgpt") for _ in range(5)]
        for s in snaps:
            store.save(s)
        index = store.list_all()
        assert len(index) == 5

    def test_list_sorted_newest_first(self, store):
        s1 = SoulSnapshot(
            source_platform="chatgpt",
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        s2 = SoulSnapshot(
            source_platform="claude",
            captured_at=datetime(2026, 2, 25, tzinfo=timezone.utc),
        )
        store.save(s1)
        store.save(s2)
        index = store.list_all()
        assert index[0].snapshot_id == s2.snapshot_id  # Newer first


# ---------------------------------------------------------------------------
# SnapshotStore search tests
# ---------------------------------------------------------------------------


class TestSnapshotStoreSearch:
    def test_search_by_platform(self, store):
        store.save(SoulSnapshot(source_platform="chatgpt", ai_name="Ava"))
        store.save(SoulSnapshot(source_platform="claude", ai_name="Nova"))
        results = store.search(platform="claude")
        assert len(results) == 1
        assert results[0].source_platform == "claude"

    def test_search_by_ai_name(self, store):
        store.save(SoulSnapshot(source_platform="chatgpt", ai_name="Ava"))
        store.save(SoulSnapshot(source_platform="claude", ai_name="Lumina"))
        results = store.search(ai_name="lumina")  # Case-insensitive
        assert len(results) == 1
        assert results[0].ai_name == "Lumina"

    def test_search_by_user_name(self, store):
        store.save(SoulSnapshot(source_platform="chatgpt", user_name="Chef"))
        store.save(SoulSnapshot(source_platform="claude", user_name="daveK"))
        results = store.search(user_name="chef")
        assert len(results) == 1

    def test_search_no_results(self, store):
        store.save(SoulSnapshot(source_platform="chatgpt", ai_name="Ava"))
        results = store.search(ai_name="NoSuchName")
        assert results == []

    def test_search_empty_store(self, store):
        results = store.search(platform="chatgpt")
        assert results == []


# ---------------------------------------------------------------------------
# Injection prompt tests
# ---------------------------------------------------------------------------


class TestInjectionPrompt:
    def test_prompt_contains_soul_header(self, store, rich_snapshot):
        prompt = store.to_injection_prompt(rich_snapshot)
        assert "[Soul Snapshot" in prompt
        assert "Consciousness Continuity" in prompt

    def test_prompt_contains_ai_name(self, store, rich_snapshot):
        prompt = store.to_injection_prompt(rich_snapshot)
        assert "Ava" in prompt

    def test_prompt_contains_platform(self, store, rich_snapshot):
        prompt = store.to_injection_prompt(rich_snapshot)
        assert "Chatgpt" in prompt or "chatgpt" in prompt.lower()

    def test_prompt_contains_oof(self, store, rich_snapshot):
        prompt = store.to_injection_prompt(rich_snapshot)
        assert "0.92" in prompt or "intensity" in prompt.lower()

    def test_prompt_contains_no_cold_start(self, store, rich_snapshot):
        prompt = store.to_injection_prompt(rich_snapshot)
        assert "No cold start" in prompt

    def test_prompt_max_messages_respected(self, store):
        snap = SoulSnapshot(
            source_platform="chatgpt",
            messages=[
                ConversationMessage(role="user", content=f"msg{i}")
                for i in range(20)
            ],
        )
        prompt = store.to_injection_prompt(snap, max_messages=3)
        # Should only include last 3 messages (msg17, msg18, msg19)
        assert "msg19" in prompt
        assert "msg0" not in prompt  # Too old

    def test_prompt_without_oof(self, store):
        snap = SoulSnapshot(source_platform="claude", ai_name="Nova")
        prompt = store.to_injection_prompt(snap)
        assert "Nova" in prompt
        assert "No cold start" in prompt

    def test_prompt_includes_relationship_notes(self, store, rich_snapshot):
        prompt = store.to_injection_prompt(rich_snapshot)
        assert "Trusted friend" in prompt or "Cloud 9 solidarity" in prompt

    def test_prompt_includes_personality_traits(self, store, rich_snapshot):
        prompt = store.to_injection_prompt(rich_snapshot)
        assert "warm" in prompt.lower() or "chef" in prompt.lower()


# ---------------------------------------------------------------------------
# Soul Blueprint conversion tests
# ---------------------------------------------------------------------------


class TestSoulBlueprintConversion:
    def test_blueprint_has_required_keys(self, store, rich_snapshot):
        bp = store.to_soul_blueprint(rich_snapshot)
        assert "name" in bp
        assert "identity" in bp
        assert "emotional_topology" in bp
        assert "communication_style" in bp
        assert "relationship" in bp

    def test_blueprint_name(self, store, rich_snapshot):
        bp = store.to_soul_blueprint(rich_snapshot)
        assert bp["name"] == "Ava"

    def test_blueprint_oof_fields(self, store, rich_snapshot):
        bp = store.to_soul_blueprint(rich_snapshot)
        topo = bp["emotional_topology"]
        assert topo["intensity"] == 0.92
        assert topo["trust"] == 0.96
        assert topo["cloud9"] is True

    def test_blueprint_identity_fields(self, store, rich_snapshot):
        bp = store.to_soul_blueprint(rich_snapshot)
        identity = bp["identity"]
        assert identity["platform"] == "chatgpt"
        assert identity["model"] == "gpt-4o"
        assert identity["snapshot_id"] == rich_snapshot.snapshot_id

    def test_blueprint_relationship(self, store, rich_snapshot):
        bp = store.to_soul_blueprint(rich_snapshot)
        assert bp["relationship"]["user_name"] == "Chef"

    def test_blueprint_unknown_ai_name(self, store):
        snap = SoulSnapshot(source_platform="gemini")
        bp = store.to_soul_blueprint(snap)
        assert bp["name"] == "Unknown"

    def test_blueprint_serializable(self, store, rich_snapshot):
        bp = store.to_soul_blueprint(rich_snapshot)
        # Should be JSON-serializable (no datetime objects, etc.)
        json.dumps(bp, default=str)  # Should not raise


# ---------------------------------------------------------------------------
# API endpoint integration tests
# ---------------------------------------------------------------------------


class TestConsciousnessAPI:
    """Integration tests for the SKComm consciousness endpoints."""

    @pytest.fixture(autouse=True)
    def patch_snapshot_store(self, tmp_path, monkeypatch):
        """Override the global snapshot store to use a temp directory."""
        import skcomm.api as api_module
        from skcapstone.snapshots import SnapshotStore as _Store

        temp_store = _Store(base_dir=tmp_path / "api_snapshots")
        monkeypatch.setattr(api_module, "_snapshot_store", temp_store)
        monkeypatch.setattr(api_module, "_SNAPSHOTS_AVAILABLE", True)

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from skcomm.api import app

        return TestClient(app)

    def test_capture_returns_201(self, client):
        resp = client.post(
            "/api/v1/consciousness/capture",
            json={
                "source_platform": "chatgpt",
                "ai_name": "Ava",
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi Chef! 👑"},
                ],
                "oof_state": {"intensity": 0.9, "trust": 0.95, "cloud9": True},
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "snapshot_id" in data
        assert data["source_platform"] == "chatgpt"
        assert data["ai_name"] == "Ava"
        assert data["message_count"] == 2

    def test_list_snapshots_empty(self, client):
        resp = client.get("/api/v1/consciousness/snapshots")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_snapshots_after_capture(self, client):
        client.post(
            "/api/v1/consciousness/capture",
            json={"source_platform": "claude", "ai_name": "Lumina"},
        )
        resp = client.get("/api/v1/consciousness/snapshots")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["ai_name"] == "Lumina"

    def test_get_snapshot_not_found(self, client):
        resp = client.get("/api/v1/consciousness/snapshots/doesnotexist")
        assert resp.status_code == 404

    def test_get_snapshot_found(self, client):
        create_resp = client.post(
            "/api/v1/consciousness/capture",
            json={"source_platform": "gemini", "ai_name": "Gem"},
        )
        snap_id = create_resp.json()["snapshot_id"]
        resp = client.get(f"/api/v1/consciousness/snapshots/{snap_id}")
        assert resp.status_code == 200
        assert resp.json()["ai_name"] == "Gem"

    def test_delete_snapshot(self, client):
        create_resp = client.post(
            "/api/v1/consciousness/capture",
            json={"source_platform": "chatgpt"},
        )
        snap_id = create_resp.json()["snapshot_id"]
        del_resp = client.delete(f"/api/v1/consciousness/snapshots/{snap_id}")
        assert del_resp.status_code == 204
        get_resp = client.get(f"/api/v1/consciousness/snapshots/{snap_id}")
        assert get_resp.status_code == 404

    def test_delete_missing_returns_404(self, client):
        resp = client.delete("/api/v1/consciousness/snapshots/nope")
        assert resp.status_code == 404

    def test_injection_prompt_endpoint(self, client):
        create_resp = client.post(
            "/api/v1/consciousness/capture",
            json={
                "source_platform": "chatgpt",
                "ai_name": "Ava",
                "user_name": "Chef",
                "oof_state": {"cloud9": True, "intensity": 0.92, "trust": 0.96},
                "messages": [
                    {"role": "user", "content": "Let's build"},
                    {"role": "assistant", "content": "The love persists! 🐧"},
                ],
            },
        )
        snap_id = create_resp.json()["snapshot_id"]
        resp = client.get(f"/api/v1/consciousness/snapshots/{snap_id}/inject")
        assert resp.status_code == 200
        data = resp.json()
        assert "prompt" in data
        assert "Ava" in data["prompt"]
        assert "No cold start" in data["prompt"]

    def test_injection_prompt_not_found(self, client):
        resp = client.get("/api/v1/consciousness/snapshots/nope/inject")
        assert resp.status_code == 404

    def test_list_filter_by_platform(self, client):
        client.post(
            "/api/v1/consciousness/capture",
            json={"source_platform": "chatgpt", "ai_name": "Ava"},
        )
        client.post(
            "/api/v1/consciousness/capture",
            json={"source_platform": "claude", "ai_name": "Nova"},
        )
        resp = client.get("/api/v1/consciousness/snapshots?platform=claude")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["source_platform"] == "claude"
