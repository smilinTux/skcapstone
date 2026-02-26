"""
Tests for skcapstone.team_comms — agent-to-agent communication layer.

Covers:
- Channel bootstrapping (directory creation)
- send_to_teammate happy path
- send_to_teammate with unknown recipient raises ValueError
- receive_messages drains inbox and archives files
- broadcast_to_team from queen delivers to all members
- broadcast_to_team from non-queen raises PermissionError
- receive_broadcast returns queen messages for non-queen members
- Board logging is invoked when a board is provided
- TeamEngine.deploy() populates comms_channel
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.team_comms import (
    TeamChannel,
    bootstrap_team_channel,
    broadcast_to_team,
    receive_broadcast,
    receive_messages,
    send_to_teammate,
    _ENVELOPE_SUFFIX,
    _build_envelope,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def comms_root(tmp_path: Path) -> Path:
    """Return a temporary comms root directory."""
    root = tmp_path / "comms"
    root.mkdir()
    return root


@pytest.fixture
def channel(comms_root: Path) -> TeamChannel:
    """A bootstrapped three-agent channel with 'alpha' as queen."""
    return bootstrap_team_channel(
        team_slug="test-team",
        agent_names=["alpha", "beta", "gamma"],
        comms_root=comms_root,
        queen="alpha",
    )


# ---------------------------------------------------------------------------
# bootstrap_team_channel
# ---------------------------------------------------------------------------


class TestBootstrapTeamChannel:
    """Tests for bootstrap_team_channel()."""

    def test_creates_inbox_and_archive_for_each_agent(
        self, comms_root: Path
    ) -> None:
        """Inbox and archive directories are created for every agent."""
        channel = bootstrap_team_channel(
            team_slug="myteam",
            agent_names=["worker1", "worker2"],
            comms_root=comms_root,
        )
        for agent in ["worker1", "worker2"]:
            assert channel.inbox_for(agent).is_dir()
            assert channel.archive_for(agent).is_dir()

    def test_broadcast_dir_created_when_queen_set(
        self, comms_root: Path
    ) -> None:
        """Broadcast directory exists when queen is specified."""
        channel = bootstrap_team_channel(
            team_slug="queenteam",
            agent_names=["queen", "minion"],
            comms_root=comms_root,
            queen="queen",
        )
        assert channel.broadcast_dir.is_dir()

    def test_broadcast_dir_not_created_without_queen(
        self, comms_root: Path
    ) -> None:
        """No broadcast directory when no queen is set."""
        channel = bootstrap_team_channel(
            team_slug="peerteam",
            agent_names=["a", "b"],
            comms_root=comms_root,
            queen=None,
        )
        assert not channel.broadcast_dir.exists()

    def test_idempotent(self, comms_root: Path) -> None:
        """Calling bootstrap twice does not raise or corrupt state."""
        bootstrap_team_channel("idempotent", ["x"], comms_root, queen="x")
        channel = bootstrap_team_channel("idempotent", ["x"], comms_root, queen="x")
        assert channel.inbox_for("x").is_dir()

    def test_channel_members_and_queen(self, comms_root: Path) -> None:
        """Channel reports correct members and queen."""
        channel = bootstrap_team_channel(
            team_slug="t",
            agent_names=["a", "b", "c"],
            comms_root=comms_root,
            queen="a",
        )
        assert channel.members == ["a", "b", "c"]
        assert channel.queen == "a"


# ---------------------------------------------------------------------------
# send_to_teammate
# ---------------------------------------------------------------------------


class TestSendToTeammate:
    """Tests for send_to_teammate()."""

    def test_happy_path_writes_envelope(self, channel: TeamChannel) -> None:
        """A valid send writes exactly one .skc.json file to the inbox."""
        send_to_teammate("alpha", "beta", "Hello beta!", channel)
        inbox = channel.inbox_for("beta")
        files = list(inbox.glob(f"*{_ENVELOPE_SUFFIX}"))
        assert len(files) == 1

    def test_envelope_content_is_valid_json(self, channel: TeamChannel) -> None:
        """The written file is parseable and has correct sender/recipient."""
        eid = send_to_teammate("alpha", "gamma", "Hi gamma", channel)
        inbox = channel.inbox_for("gamma")
        files = list(inbox.glob(f"*{_ENVELOPE_SUFFIX}"))
        data = json.loads(files[0].read_text())
        assert data["sender"] == "alpha"
        assert data["recipient"] == "gamma"
        assert data["payload"]["content"] == "Hi gamma"
        assert data["envelope_id"] == eid

    def test_thread_id_propagated(self, channel: TeamChannel) -> None:
        """thread_id is stored in envelope metadata."""
        send_to_teammate("beta", "gamma", "msg", channel, thread_id="t-001")
        files = list(channel.inbox_for("gamma").glob(f"*{_ENVELOPE_SUFFIX}"))
        data = json.loads(files[0].read_text())
        assert data["metadata"]["thread_id"] == "t-001"

    def test_unknown_recipient_raises(self, channel: TeamChannel) -> None:
        """Sending to a non-member raises ValueError."""
        with pytest.raises(ValueError, match="not a member"):
            send_to_teammate("alpha", "unknown-bot", "hi", channel)

    def test_board_log_called(self, channel: TeamChannel) -> None:
        """Board.save_agent is called when board is provided."""
        mock_board = MagicMock()
        mock_board.load_agent.return_value = None
        send_to_teammate("alpha", "beta", "msg", channel, board=mock_board)
        mock_board.save_agent.assert_called_once()

    def test_board_failure_does_not_raise(self, channel: TeamChannel) -> None:
        """A broken board never prevents message delivery."""
        broken_board = MagicMock()
        broken_board.load_agent.side_effect = RuntimeError("db down")
        # Should not raise
        eid = send_to_teammate("alpha", "beta", "msg", channel, board=broken_board)
        assert eid


# ---------------------------------------------------------------------------
# receive_messages
# ---------------------------------------------------------------------------


class TestReceiveMessages:
    """Tests for receive_messages()."""

    def test_returns_empty_when_no_messages(self, channel: TeamChannel) -> None:
        """Empty inbox returns empty list."""
        assert receive_messages("beta", channel) == []

    def test_drains_all_messages(self, channel: TeamChannel) -> None:
        """All messages in inbox are returned and then archived."""
        send_to_teammate("alpha", "beta", "msg1", channel)
        send_to_teammate("gamma", "beta", "msg2", channel)
        received = receive_messages("beta", channel)
        assert len(received) == 2
        # Inbox now empty
        assert receive_messages("beta", channel) == []

    def test_messages_moved_to_archive(self, channel: TeamChannel) -> None:
        """Processed messages are moved to archive, not deleted."""
        send_to_teammate("alpha", "beta", "archived?", channel)
        receive_messages("beta", channel)
        archive = channel.archive_for("beta")
        files = list(archive.glob(f"*{_ENVELOPE_SUFFIX}"))
        assert len(files) == 1

    def test_returns_correct_content(self, channel: TeamChannel) -> None:
        """Returned envelopes contain the correct message content."""
        send_to_teammate("alpha", "beta", "specific content", channel)
        msgs = receive_messages("beta", channel)
        assert msgs[0]["payload"]["content"] == "specific content"

    def test_corrupted_file_skipped(self, channel: TeamChannel) -> None:
        """A malformed envelope file is skipped without crashing."""
        inbox = channel.inbox_for("beta")
        inbox.mkdir(parents=True, exist_ok=True)
        bad_file = inbox / f"corrupt{_ENVELOPE_SUFFIX}"
        bad_file.write_text("not valid json")
        # Valid message also present
        send_to_teammate("alpha", "beta", "valid", channel)
        msgs = receive_messages("beta", channel)
        assert len(msgs) == 1
        assert msgs[0]["payload"]["content"] == "valid"


# ---------------------------------------------------------------------------
# broadcast_to_team
# ---------------------------------------------------------------------------


class TestBroadcastToTeam:
    """Tests for broadcast_to_team()."""

    def test_queen_can_broadcast(self, channel: TeamChannel) -> None:
        """Queen can broadcast; all non-queen members receive the message."""
        eids = broadcast_to_team("alpha", "All hands!", channel)
        assert len(eids) == 2  # beta and gamma
        assert len(receive_messages("beta", channel)) == 1
        assert len(receive_messages("gamma", channel)) == 1

    def test_queen_does_not_receive_own_broadcast(
        self, channel: TeamChannel
    ) -> None:
        """The queen's inbox is not written to during a broadcast."""
        broadcast_to_team("alpha", "Hello team", channel)
        assert receive_messages("alpha", channel) == []

    def test_broadcast_written_to_audit_dir(self, channel: TeamChannel) -> None:
        """Broadcast envelope is also written to the broadcast directory."""
        broadcast_to_team("alpha", "Audit this", channel)
        files = list(channel.broadcast_dir.glob(f"*{_ENVELOPE_SUFFIX}"))
        assert len(files) == 1

    def test_non_queen_broadcast_raises(self, channel: TeamChannel) -> None:
        """Non-queen agent attempting broadcast raises PermissionError."""
        with pytest.raises(PermissionError, match="queen"):
            broadcast_to_team("beta", "I am not the queen", channel)

    def test_no_queen_allows_any_broadcast(self, comms_root: Path) -> None:
        """When channel has no queen, any member may broadcast."""
        ch = bootstrap_team_channel(
            "open-team", ["x", "y", "z"], comms_root, queen=None
        )
        eids = broadcast_to_team("x", "peer broadcast", ch)
        # y and z each get one message
        assert len(eids) == 2


# ---------------------------------------------------------------------------
# receive_broadcast
# ---------------------------------------------------------------------------


class TestReceiveBroadcast:
    """Tests for receive_broadcast()."""

    def test_non_queen_reads_broadcast(self, channel: TeamChannel) -> None:
        """A member agent can read broadcast messages."""
        broadcast_to_team("alpha", "Broadcast content", channel)
        msgs = receive_broadcast("beta", channel)
        assert len(msgs) == 1
        assert msgs[0]["payload"]["content"] == "Broadcast content"

    def test_sender_filtered_from_broadcast(self, channel: TeamChannel) -> None:
        """The queen's own broadcast is not returned to herself."""
        broadcast_to_team("alpha", "self-filter test", channel)
        msgs = receive_broadcast("alpha", channel)
        assert msgs == []

    def test_empty_when_no_broadcasts(self, channel: TeamChannel) -> None:
        """Returns empty list when broadcast directory is empty."""
        assert receive_broadcast("beta", channel) == []


# ---------------------------------------------------------------------------
# _build_envelope
# ---------------------------------------------------------------------------


class TestBuildEnvelope:
    """Tests for the internal _build_envelope helper."""

    def test_required_fields_present(self) -> None:
        """Envelope dict has all required SKComm fields."""
        env = _build_envelope("agent-a", "agent-b", "hello")
        assert env["skcomm_version"] == "1.0.0"
        assert env["sender"] == "agent-a"
        assert env["recipient"] == "agent-b"
        assert env["payload"]["content"] == "hello"
        assert "envelope_id" in env

    def test_unique_ids(self) -> None:
        """Two envelopes have distinct envelope_ids."""
        e1 = _build_envelope("a", "b", "msg")
        e2 = _build_envelope("a", "b", "msg")
        assert e1["envelope_id"] != e2["envelope_id"]


# ---------------------------------------------------------------------------
# TeamEngine integration
# ---------------------------------------------------------------------------


class TestTeamEngineCommsIntegration:
    """Verify TeamEngine.deploy() wires comms correctly."""

    def test_deploy_populates_comms_channel(self, tmp_path: Path) -> None:
        """After deploy(), TeamDeployment.comms_channel is a TeamChannel."""
        from skcapstone.blueprints.schema import (
            AgentRole,
            AgentSpec,
            BlueprintManifest,
            CoordinationConfig,
        )
        from skcapstone.team_engine import TeamEngine

        blueprint = BlueprintManifest(
            name="Test Team",
            slug="test-team",
            description="Unit test team",
            agents={
                "queen": AgentSpec(role=AgentRole.MANAGER),
                "worker": AgentSpec(role=AgentRole.WORKER),
            },
            coordination=CoordinationConfig(queen="queen"),
        )

        engine = TeamEngine(
            home=tmp_path / ".skcapstone",
            comms_root=tmp_path / "comms",
        )
        deployment = engine.deploy(blueprint)

        assert deployment.comms_channel is not None
        assert isinstance(deployment.comms_channel, TeamChannel)
        assert len(deployment.comms_channel.members) == 2

    def test_deploy_without_comms_root_skips_channel(
        self, tmp_path: Path
    ) -> None:
        """When comms_root is explicitly None, comms_channel stays None."""
        from skcapstone.blueprints.schema import AgentSpec, BlueprintManifest
        from skcapstone.team_engine import TeamEngine

        blueprint = BlueprintManifest(
            name="No Comms",
            slug="no-comms",
            description="Team without comms",
            agents={"solo": AgentSpec()},
        )

        # Monkeypatch: set _comms_root to None directly after construction
        engine = TeamEngine(home=tmp_path / ".skcapstone")
        engine._comms_root = None
        deployment = engine.deploy(blueprint)

        assert deployment.comms_channel is None

    def test_deploy_queen_identified_from_role(self, tmp_path: Path) -> None:
        """Queen is detected from AgentRole.MANAGER when coordination.queen unset."""
        from skcapstone.blueprints.schema import (
            AgentRole,
            AgentSpec,
            BlueprintManifest,
            CoordinationConfig,
        )
        from skcapstone.team_engine import TeamEngine

        blueprint = BlueprintManifest(
            name="Role Team",
            slug="role-team",
            description="Queen via role",
            agents={
                "boss": AgentSpec(role=AgentRole.MANAGER),
                "minion": AgentSpec(role=AgentRole.WORKER),
            },
            coordination=CoordinationConfig(queen=None),
        )

        engine = TeamEngine(
            home=tmp_path / ".skcapstone",
            comms_root=tmp_path / "comms",
        )
        deployment = engine.deploy(blueprint)
        channel = deployment.comms_channel
        assert channel is not None
        assert channel.queen is not None
        assert "boss" in channel.queen
