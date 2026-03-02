"""
Tests for the full Syncthing ↔ consciousness loop comms pipeline.

Pipeline under test:
    {shared_root}/sync/comms/inbox/{peer}/*.skc.json   ← Syncthing writes here
    → inotify detects file  (ConsciousnessLoop._run_inotify)
    → ConsciousnessLoop.process_envelope()
    → skcomm.send(sender, response)
    → {shared_root}/sync/comms/outbox/{peer}/*.skc.json ← Syncthing syncs this out

Three test classes:
    TestInboxToOutboxFlow      — mock LLM, drop inbox file, verify skcomm.send called
    TestOutboxEnvelopeFormat   — verify envelope spec compliance and path layout
    TestSyncStatusInHealth     — verify health snapshot includes sync_pipeline key
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.sync_engine import (
    ENVELOPE_SUFFIX,
    get_inbox_dir,
    get_outbox_dir,
    get_sync_pipeline_status,
    verify_pipeline_paths,
    write_outbox_envelope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_root(tmp_path: Path) -> Path:
    """Minimal shared-root with comms inbox/outbox directories pre-created."""
    root = tmp_path / ".skcapstone"
    root.mkdir()
    (root / "sync" / "comms" / "inbox").mkdir(parents=True)
    (root / "sync" / "comms" / "outbox").mkdir(parents=True)
    return root


@pytest.fixture
def agent_home(shared_root: Path) -> Path:
    """Single-agent mode: home == shared_root.  Provides identity.json."""
    home = shared_root
    for d in ("identity", "memory", "trust", "config", "conversations"):
        (home / d).mkdir(exist_ok=True)
    (home / "identity" / "identity.json").write_text(
        json.dumps({"name": "TestAgent", "fingerprint": "AABB1122CCDD3344"}),
        encoding="utf-8",
    )
    return home


@pytest.fixture
def mock_skcomm() -> MagicMock:
    skcomm = MagicMock()
    skcomm.send.return_value = None
    skcomm.receive.return_value = []
    return skcomm


# ---------------------------------------------------------------------------
# TestInboxToOutboxFlow
# ---------------------------------------------------------------------------


class TestInboxToOutboxFlow:
    """Drop a file in the inbox and verify the consciousness loop responds."""

    def _make_loop(self, agent_home: Path, shared_root: Path, mock_skcomm):
        """Return a ConsciousnessLoop with a mocked LLM bridge."""
        from skcapstone.consciousness_loop import ConsciousnessConfig, ConsciousnessLoop

        config = ConsciousnessConfig(
            enabled=True,
            use_inotify=False,       # manual trigger in tests
            auto_ack=False,
            auto_memory=False,
            desktop_notifications=False,
        )

        with patch("skcapstone.consciousness_loop.LLMBridge") as MockBridge:
            bridge = MagicMock()
            bridge.generate.return_value = "Hello back from TestAgent"
            bridge.available_backends = {"passthrough": True}
            MockBridge.return_value = bridge

            loop = ConsciousnessLoop(
                config,
                home=agent_home,
                shared_root=shared_root,
            )

        loop.set_skcomm(mock_skcomm)
        return loop

    def test_inbox_to_outbox_flow(
        self, shared_root: Path, agent_home: Path, mock_skcomm: MagicMock
    ):
        """File dropped in inbox triggers process_envelope and skcomm.send."""
        loop = self._make_loop(agent_home, shared_root, mock_skcomm)

        peer = "alice"
        inbox_peer = get_inbox_dir(shared_root) / peer
        inbox_peer.mkdir(parents=True, exist_ok=True)

        envelope = {
            "envelope_id": "test-msg-001",
            "sender": peer,
            "recipient": "testagent",
            "timestamp": "2026-03-01T00:00:00Z",
            "payload": {
                "content": "Hey TestAgent, what is happening?",
                "content_type": "text",
            },
        }
        inbox_file = inbox_peer / f"test-msg-001{ENVELOPE_SUFFIX}"
        inbox_file.write_text(json.dumps(envelope), encoding="utf-8")

        # Simulate inotify trigger
        loop._on_inbox_file(inbox_file)
        loop._executor.shutdown(wait=True)

        assert mock_skcomm.send.called, "skcomm.send() must be called with the LLM response"
        recipient_arg = mock_skcomm.send.call_args[0][0]
        assert recipient_arg == peer, "Response must be addressed to the original sender"

    def test_skipped_when_sender_missing(
        self, shared_root: Path, agent_home: Path, mock_skcomm: MagicMock
    ):
        """Envelopes without a sender field are silently dropped."""
        loop = self._make_loop(agent_home, shared_root, mock_skcomm)

        inbox_peer = get_inbox_dir(shared_root) / "ghost"
        inbox_peer.mkdir(parents=True, exist_ok=True)
        no_sender = {"payload": {"content": "ghost message", "content_type": "text"}}
        inbox_file = inbox_peer / f"no-sender{ENVELOPE_SUFFIX}"
        inbox_file.write_text(json.dumps(no_sender), encoding="utf-8")

        loop._on_inbox_file(inbox_file)
        loop._executor.shutdown(wait=True)

        mock_skcomm.send.assert_not_called()

    def test_duplicate_envelope_not_reprocessed(
        self, shared_root: Path, agent_home: Path, mock_skcomm: MagicMock
    ):
        """The same envelope_id is never processed twice."""
        loop = self._make_loop(agent_home, shared_root, mock_skcomm)

        peer = "bob"
        inbox_peer = get_inbox_dir(shared_root) / peer
        inbox_peer.mkdir(parents=True, exist_ok=True)

        envelope = {
            "envelope_id": "dup-001",
            "sender": peer,
            "recipient": "testagent",
            "payload": {"content": "duplicate test", "content_type": "text"},
        }
        inbox_file = inbox_peer / f"dup-001{ENVELOPE_SUFFIX}"
        inbox_file.write_text(json.dumps(envelope), encoding="utf-8")

        # First call
        loop._on_inbox_file(inbox_file)
        loop._executor.shutdown(wait=True)
        first_count = mock_skcomm.send.call_count

        # Recreate the loop's executor for a second submission attempt
        from concurrent.futures import ThreadPoolExecutor
        loop._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="consciousness")

        # Second call with the same file (same envelope_id)
        loop._on_inbox_file(inbox_file)
        loop._executor.shutdown(wait=True)

        # Exactly the same number of sends — second call was deduped
        assert mock_skcomm.send.call_count == first_count


# ---------------------------------------------------------------------------
# TestOutboxEnvelopeFormat
# ---------------------------------------------------------------------------


class TestOutboxEnvelopeFormat:
    """Verify that outbox envelopes comply with the SKComm envelope spec."""

    def test_required_fields_preserved(self, shared_root: Path):
        """write_outbox_envelope writes all envelope fields to the .skc.json."""
        envelope = {
            "envelope_id": "out-001",
            "sender": "opus",
            "recipient": "alice",
            "timestamp": "2026-03-01T00:00:00Z",
            "payload": {
                "content": "Response from Opus",
                "content_type": "text",
            },
        }
        written = write_outbox_envelope(shared_root, "alice", envelope)

        assert written.exists()
        data = json.loads(written.read_text(encoding="utf-8"))
        assert data["envelope_id"] == "out-001"
        assert data["sender"] == "opus"
        assert data["recipient"] == "alice"
        assert data["payload"]["content"] == "Response from Opus"
        assert data["payload"]["content_type"] == "text"

    def test_file_placed_in_outbox_subdirectory(self, shared_root: Path):
        """Envelope file lives under outbox/{recipient}/."""
        envelope = {"envelope_id": "dir-test", "payload": {"content": "hi", "content_type": "text"}}
        written = write_outbox_envelope(shared_root, "bob", envelope)

        expected_parent = get_outbox_dir(shared_root) / "bob"
        assert written.parent == expected_parent

    def test_file_has_skc_json_suffix(self, shared_root: Path):
        """Written file ends with .skc.json."""
        envelope = {"envelope_id": "suffix-test", "payload": {"content": "x", "content_type": "text"}}
        written = write_outbox_envelope(shared_root, "carol", envelope)
        assert written.name.endswith(ENVELOPE_SUFFIX)

    def test_atomic_write_leaves_no_tmp_files(self, shared_root: Path):
        """No .tmp files remain after write_outbox_envelope."""
        envelope = {
            "envelope_id": "atomic-001",
            "payload": {"content": "atomic test", "content_type": "text"},
        }
        written = write_outbox_envelope(shared_root, "dave", envelope)
        tmp_files = list(written.parent.glob("*.tmp"))
        assert tmp_files == []

    def test_path_traversal_in_recipient_neutralized(self, shared_root: Path):
        """Path-traversal characters in recipient are stripped."""
        envelope = {
            "envelope_id": "safe-001",
            "payload": {"content": "test", "content_type": "text"},
        }
        written = write_outbox_envelope(shared_root, "../../../etc/passwd", envelope)
        # Must stay inside shared_root
        assert shared_root in written.parents

    def test_outbox_pipeline_status_reflects_written_file(self, shared_root: Path):
        """get_sync_pipeline_status counts the newly written outbox file."""
        envelope = {
            "envelope_id": "count-001",
            "payload": {"content": "counting", "content_type": "text"},
        }
        write_outbox_envelope(shared_root, "eve", envelope)
        status = get_sync_pipeline_status(shared_root)
        assert status["outbox_files"] >= 1
        assert "eve" in status["outbox_peers"]


# ---------------------------------------------------------------------------
# TestSyncStatusInHealth
# ---------------------------------------------------------------------------


class TestSyncStatusInHealth:
    """Verify that the daemon health snapshot includes sync pipeline status."""

    def test_get_sync_pipeline_status_keys(self, shared_root: Path):
        """get_sync_pipeline_status returns all required keys."""
        status = get_sync_pipeline_status(shared_root)
        required = {
            "inbox_files",
            "outbox_files",
            "inbox_peers",
            "outbox_peers",
            "inbox_path",
            "outbox_path",
            "inbox_exists",
            "outbox_exists",
            "checked_at",
        }
        assert required.issubset(status.keys())

    def test_get_sync_pipeline_status_counts_inbox_file(self, shared_root: Path):
        """Status correctly counts a file dropped in the inbox."""
        peer = "frank"
        inbox_dir = get_inbox_dir(shared_root) / peer
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / f"msg-001{ENVELOPE_SUFFIX}").write_text(
            json.dumps({"payload": {"content": "hi"}}), encoding="utf-8"
        )

        status = get_sync_pipeline_status(shared_root)
        assert status["inbox_files"] >= 1
        assert peer in status["inbox_peers"]

    def test_verify_pipeline_paths_ok_when_dirs_exist(self, shared_root: Path):
        """verify_pipeline_paths reports no issues when dirs are present."""
        result = verify_pipeline_paths(shared_root)
        assert result["inbox_ok"] is True
        assert result["outbox_ok"] is True
        assert result["issues"] == []

    def test_verify_pipeline_paths_reports_missing_inbox(self, tmp_path: Path):
        """verify_pipeline_paths reports missing inbox dir."""
        empty_root = tmp_path / "empty"
        empty_root.mkdir()
        result = verify_pipeline_paths(empty_root)
        assert result["inbox_ok"] is False
        assert any("Inbox" in issue for issue in result["issues"])

    def test_verify_pipeline_paths_reports_missing_outbox(self, tmp_path: Path):
        """verify_pipeline_paths reports missing outbox dir."""
        root = tmp_path / "partial"
        root.mkdir()
        # create only inbox
        (root / "sync" / "comms" / "inbox").mkdir(parents=True)
        result = verify_pipeline_paths(root)
        assert result["outbox_ok"] is False
        assert any("Outbox" in issue for issue in result["issues"])

    def test_daemon_state_snapshot_includes_sync_pipeline(self):
        """DaemonState.snapshot() includes sync_pipeline after record_sync_pipeline."""
        from skcapstone.daemon import DaemonState

        state = DaemonState()
        state.record_sync_pipeline({"inbox_files": 3, "outbox_files": 1})
        snap = state.snapshot()

        assert "sync_pipeline" in snap
        assert snap["sync_pipeline"]["inbox_files"] == 3
        assert snap["sync_pipeline"]["outbox_files"] == 1

    def test_daemon_state_sync_pipeline_starts_empty(self):
        """DaemonState starts with an empty sync_pipeline dict."""
        from skcapstone.daemon import DaemonState

        state = DaemonState()
        snap = state.snapshot()
        assert "sync_pipeline" in snap
        assert snap["sync_pipeline"] == {}

    def test_verify_pipeline_detects_transport_misalignment(self, shared_root: Path):
        """verify_pipeline_paths flags a SyncthingTransport with a wrong comms_root."""
        # Build a mock SKComm with a Syncthing transport pointing to the wrong path
        wrong_transport = MagicMock()
        wrong_transport.name = "syncthing"
        wrong_transport._root = Path("/tmp/wrong/path")

        mock_router = MagicMock()
        mock_router.transports = [wrong_transport]

        mock_skcomm = MagicMock()
        mock_skcomm.router = mock_router

        result = verify_pipeline_paths(shared_root, skcomm=mock_skcomm)
        assert result["transport_aligned"] is False
        assert any("mismatch" in issue.lower() for issue in result["issues"])
