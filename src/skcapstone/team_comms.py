"""
Team Communications — SKComm/SKChat wiring for deployed agent teams.

Bootstraps a local file-based SKComm channel for each deployed team so that
agents can message each other without external infrastructure. Each agent gets
its own inbox directory; messages are plain JSON envelopes written atomically
to the recipient's inbox folder.

Directory layout inside a team's comms directory:
    <comms_root>/<team_slug>/
    ├── <agent_name>/
    │   ├── inbox/       # Incoming .skc.json envelope files
    │   └── archive/     # Processed envelopes (kept for audit)
    └── broadcast/       # Queen's broadcast channel (all members read here)

Coordination board integration: every sent/received message is logged as a
note on the sending agent's AgentFile so the board reflects comms activity.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Sentinel value: the queen/manager role name used to discover the broadcast sender
_QUEEN_ROLES = frozenset({"manager", "queen"})

# File suffix used by the SKComm file transport
_ENVELOPE_SUFFIX = ".skc.json"


# ---------------------------------------------------------------------------
# Channel configuration model
# ---------------------------------------------------------------------------


class TeamChannel(BaseModel):
    """Configuration for a team's comms channel.

    Attributes:
        team_slug: Filesystem-safe identifier for the team.
        comms_root: Root directory for all team comms.
        members: List of agent names with registered inboxes.
        queen: Optional manager/queen agent name with broadcast rights.
    """

    team_slug: str
    comms_root: Path
    members: List[str] = Field(default_factory=list)
    queen: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True

    @property
    def team_dir(self) -> Path:
        """Root directory for this specific team's comms."""
        return self.comms_root / self.team_slug

    def inbox_for(self, agent_name: str) -> Path:
        """Return the inbox path for a given agent.

        Args:
            agent_name: The agent whose inbox path to return.

        Returns:
            Path to the agent's inbox directory.
        """
        return self.team_dir / agent_name / "inbox"

    def archive_for(self, agent_name: str) -> Path:
        """Return the archive path for a given agent.

        Args:
            agent_name: The agent whose archive path to return.

        Returns:
            Path to the agent's archive directory.
        """
        return self.team_dir / agent_name / "archive"

    @property
    def broadcast_dir(self) -> Path:
        """Shared broadcast directory written by the queen."""
        return self.team_dir / "broadcast"


# ---------------------------------------------------------------------------
# Outgoing envelope helper
# ---------------------------------------------------------------------------


def _build_envelope(
    sender: str,
    recipient: str,
    content: str,
    thread_id: Optional[str] = None,
) -> dict:
    """Build a minimal SKComm-compatible envelope dict.

    Uses the same schema as skcomm.models.MessageEnvelope so downstream
    consumers can deserialize with MessageEnvelope.from_bytes().

    Args:
        sender: Agent name of the sender.
        recipient: Agent name of the recipient (or "broadcast").
        content: Plain-text message content.
        thread_id: Optional thread identifier for grouping messages.

    Returns:
        dict: Envelope ready for JSON serialisation.
    """
    now = datetime.now(timezone.utc).isoformat()
    return {
        "skcomm_version": "1.0.0",
        "envelope_id": str(uuid.uuid4()),
        "sender": sender,
        "recipient": recipient,
        "payload": {
            "content": content,
            "content_type": "text",
            "encrypted": False,
            "compressed": False,
            "signature": None,
        },
        "routing": {
            "mode": "failover",
            "preferred_transports": ["file"],
            "retry_max": 3,
            "retry_backoff": [5, 15, 60],
            "ttl": 86400,
            "ack_requested": False,
        },
        "metadata": {
            "thread_id": thread_id,
            "in_reply_to": None,
            "urgency": "normal",
            "created_at": now,
            "expires_at": None,
            "attempt": 0,
            "delivered_via": "file",
        },
    }


def _write_envelope(inbox_dir: Path, envelope: dict) -> Path:
    """Atomically write an envelope to an inbox directory.

    Uses a .tmp rename strategy to prevent partial reads.

    Args:
        inbox_dir: Target inbox directory.
        envelope: Envelope dict to serialise.

    Returns:
        Path to the written envelope file.
    """
    inbox_dir.mkdir(parents=True, exist_ok=True)
    envelope_id = envelope.get("envelope_id", str(uuid.uuid4()))
    filename = f"{envelope_id}{_ENVELOPE_SUFFIX}"
    target = inbox_dir / filename
    tmp = inbox_dir / f".{filename}.tmp"
    data = json.dumps(envelope, indent=2).encode("utf-8")
    tmp.write_bytes(data)
    tmp.rename(target)
    return target


def _drain_inbox(agent_name: str, channel: TeamChannel) -> List[dict]:
    """Drain all pending envelopes from an agent's inbox.

    Moves processed files to archive. Skips tmp files and corrupted JSON.

    Args:
        agent_name: The agent whose inbox to drain.
        channel: The TeamChannel providing directory paths.

    Returns:
        List of envelope dicts read from the inbox.
    """
    inbox = channel.inbox_for(agent_name)
    archive = channel.archive_for(agent_name)

    if not inbox.exists():
        return []

    envelopes: List[dict] = []
    for env_file in sorted(inbox.glob(f"*{_ENVELOPE_SUFFIX}")):
        if env_file.name.startswith("."):
            continue
        try:
            data = json.loads(env_file.read_bytes())
            envelopes.append(data)
            # Move to archive
            archive.mkdir(parents=True, exist_ok=True)
            dest = archive / env_file.name
            if dest.exists():
                dest = archive / f"{int(time.time())}-{env_file.name}"
            env_file.rename(dest)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping %s: %s", env_file, exc)

    return envelopes


# ---------------------------------------------------------------------------
# Channel bootstrap
# ---------------------------------------------------------------------------


def bootstrap_team_channel(
    team_slug: str,
    agent_names: List[str],
    comms_root: Path,
    queen: Optional[str] = None,
) -> TeamChannel:
    """Create the directory structure for a team comms channel.

    Idempotent: safe to call multiple times for the same team.
    Creates inbox and archive dirs for every agent plus the broadcast
    directory if a queen is specified.

    Args:
        team_slug: Filesystem-safe team identifier (matches deployment_id prefix).
        agent_names: Ordered list of agent instance names.
        comms_root: Root path under which team directories are created.
        queen: Name of the managing/queen agent that gets broadcast rights.

    Returns:
        TeamChannel: Configured channel ready for send/receive operations.
    """
    channel = TeamChannel(
        team_slug=team_slug,
        comms_root=comms_root,
        members=list(agent_names),
        queen=queen,
    )

    for agent in agent_names:
        channel.inbox_for(agent).mkdir(parents=True, exist_ok=True)
        channel.archive_for(agent).mkdir(parents=True, exist_ok=True)

    if queen:
        channel.broadcast_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Bootstrapped comms channel for team '%s' (%d agents, queen=%s)",
        team_slug,
        len(agent_names),
        queen or "none",
    )
    return channel


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_to_teammate(
    from_agent: str,
    to_agent: str,
    message: str,
    channel: TeamChannel,
    thread_id: Optional[str] = None,
    board: Optional[object] = None,
) -> str:
    """Send a message from one agent to another within the same team.

    Writes a SKComm-compatible envelope JSON file to the recipient's inbox
    directory. Optionally logs the activity to the coordination board.

    Args:
        from_agent: Sender agent name.
        to_agent: Recipient agent name.
        message: Plain-text message content.
        channel: The TeamChannel for this team.
        thread_id: Optional thread identifier for grouping related messages.
        board: Optional Board instance for activity logging.

    Returns:
        str: The envelope_id of the sent message.

    Raises:
        ValueError: If to_agent is not a member of the channel.
    """
    if to_agent not in channel.members:
        raise ValueError(
            f"Agent '{to_agent}' is not a member of team '{channel.team_slug}'. "
            f"Known members: {channel.members}"
        )

    envelope = _build_envelope(from_agent, to_agent, message, thread_id)
    inbox = channel.inbox_for(to_agent)
    path = _write_envelope(inbox, envelope)

    logger.info(
        "Agent '%s' -> '%s': %s (envelope %s)",
        from_agent,
        to_agent,
        message[:60],
        envelope["envelope_id"][:8],
    )

    if board is not None:
        _log_to_board(board, from_agent, f"msg→{to_agent}: {message[:80]}")

    return envelope["envelope_id"]


def broadcast_to_team(
    from_agent: str,
    message: str,
    channel: TeamChannel,
    thread_id: Optional[str] = None,
    board: Optional[object] = None,
) -> List[str]:
    """Broadcast a message to all team members from the queen/manager.

    Writes the same envelope to every member's inbox AND to the shared
    broadcast directory for auditability.

    Args:
        from_agent: The broadcasting agent (typically the queen).
        message: Plain-text message content.
        channel: The TeamChannel for this team.
        thread_id: Optional thread identifier.
        board: Optional Board instance for activity logging.

    Returns:
        List[str]: envelope_ids for each delivered message.

    Raises:
        PermissionError: If from_agent is not the channel's queen.
    """
    if channel.queen and from_agent != channel.queen:
        raise PermissionError(
            f"Only the queen agent ('{channel.queen}') may broadcast. "
            f"Got: '{from_agent}'"
        )

    # Write to broadcast audit log
    broadcast_envelope = _build_envelope(from_agent, "broadcast", message, thread_id)
    _write_envelope(channel.broadcast_dir, broadcast_envelope)

    envelope_ids: List[str] = []
    for member in channel.members:
        if member == from_agent:
            continue
        env = _build_envelope(from_agent, member, message, thread_id)
        inbox = channel.inbox_for(member)
        _write_envelope(inbox, env)
        envelope_ids.append(env["envelope_id"])

    logger.info(
        "Queen '%s' broadcast to %d members: %s",
        from_agent,
        len(envelope_ids),
        message[:60],
    )

    if board is not None:
        _log_to_board(
            board, from_agent, f"broadcast to {len(envelope_ids)} members: {message[:80]}"
        )

    return envelope_ids


def receive_messages(
    agent_name: str,
    channel: TeamChannel,
    board: Optional[object] = None,
) -> List[dict]:
    """Receive all pending messages for an agent.

    Drains the agent's inbox and returns envelopes as dicts.
    Processed files are moved to archive. Optionally logs activity to board.

    Args:
        agent_name: The agent polling for messages.
        channel: The TeamChannel providing inbox paths.
        board: Optional Board instance for activity logging.

    Returns:
        List[dict]: Raw envelope dicts (skcomm MessageEnvelope schema).
    """
    envelopes = _drain_inbox(agent_name, channel)

    if envelopes:
        logger.debug(
            "Agent '%s' received %d message(s)", agent_name, len(envelopes)
        )
        if board is not None:
            senders = {e.get("sender", "unknown") for e in envelopes}
            _log_to_board(
                board,
                agent_name,
                f"received {len(envelopes)} msg(s) from {', '.join(sorted(senders))}",
            )

    return envelopes


def receive_broadcast(
    agent_name: str,
    channel: TeamChannel,
) -> List[dict]:
    """Read broadcast messages from the team's broadcast directory.

    Reads but does NOT archive broadcast messages (they are shared/read-only
    from a multi-consumer perspective). Callers must track what they've read.

    Args:
        agent_name: The reading agent (used for filtering self-sent messages).
        channel: The TeamChannel providing the broadcast directory path.

    Returns:
        List[dict]: Broadcast envelope dicts not sent by this agent.
    """
    broadcast_dir = channel.broadcast_dir
    if not broadcast_dir.exists():
        return []

    envelopes: List[dict] = []
    for env_file in sorted(broadcast_dir.glob(f"*{_ENVELOPE_SUFFIX}")):
        if env_file.name.startswith("."):
            continue
        try:
            data = json.loads(env_file.read_bytes())
            if data.get("sender") != agent_name:
                envelopes.append(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping broadcast file %s: %s", env_file, exc)

    return envelopes


# ---------------------------------------------------------------------------
# Board integration helper
# ---------------------------------------------------------------------------


def _log_to_board(board: object, agent_name: str, note: str) -> None:
    """Append a comms activity note to an agent's coordination board file.

    Silently no-ops if the board doesn't support the required interface,
    so team_comms never hard-depends on skcapstone.coordination at import time.

    Args:
        board: A Board instance (duck-typed to avoid circular imports).
        agent_name: Agent whose file should be updated.
        note: Short activity description to append.
    """
    try:
        from .coordination import AgentFile

        agent_file = board.load_agent(agent_name) or AgentFile(agent=agent_name)  # type: ignore[attr-defined]
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        existing = agent_file.notes or ""
        # Reason: keep notes bounded — prepend newest, cap at 1200 chars
        new_entry = f"[{timestamp}] {note}"
        combined = f"{new_entry}\n{existing}" if existing else new_entry
        agent_file.notes = combined[:1200]
        board.save_agent(agent_file)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("Board log skipped (%s): %s", agent_name, exc)
