"""
Interactive agent-to-agent chat for the sovereign terminal.

Provides a real-time terminal chat experience between agents using
SKChat for message models and SKComm for transport. Works from any
terminal on any platform — no IDE dependency.

Usage:
    skcapstone chat send <peer> "message"
    skcapstone chat inbox
    skcapstone chat live <peer>
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.chat")


class AgentChat:
    """Interactive chat engine for sovereign agent communication.

    Wraps SKChat models and SKComm transport into a simple
    send/receive/poll interface suitable for terminal use.

    Args:
        home: Agent home directory (~/.skcapstone).
        identity: Local agent identity string.
    """

    def __init__(self, home: Path, identity: str = "unknown") -> None:
        self.home = home
        self.identity = identity
        self._comm = None
        self._history = None

    def _ensure_comm(self) -> bool:
        """Lazily initialize the SKComm engine.

        Returns:
            bool: True if communication layer is available.
        """
        if self._comm is not None:
            return True

        try:
            from skcomm.core import SKComm

            self._comm = SKComm.from_config()
            return len(self._comm.router.transports) > 0
        except ImportError:
            logger.info("skcomm not installed")
            return False
        except Exception as exc:
            logger.info("SKComm init failed: %s", exc)
            return False

    def _ensure_history(self):
        """Lazily initialize the chat history store.

        Returns:
            ChatHistory or None.
        """
        if self._history is not None:
            return self._history

        try:
            from skchat.history import ChatHistory
            from skmemory import MemoryStore

            store = MemoryStore()
            self._history = ChatHistory(store=store)
            return self._history
        except ImportError:
            return None

    def send(
        self,
        recipient: str,
        message: str,
        thread_id: Optional[str] = None,
    ) -> dict:
        """Send a message to a peer agent.

        Stores locally in SKMemory-backed history and delivers via
        SKComm if transports are available.

        Args:
            recipient: Peer agent name or CapAuth identity.
            message: Message content.
            thread_id: Optional conversation thread.

        Returns:
            dict: Result with 'stored', 'delivered', 'transport' keys.
        """
        result = {"stored": False, "delivered": False, "transport": None, "error": None}

        try:
            from skchat.models import ChatMessage, DeliveryStatus

            msg = ChatMessage(
                sender=self.identity,
                recipient=recipient,
                content=message,
                thread_id=thread_id,
                delivery_status=DeliveryStatus.PENDING,
            )

            history = self._ensure_history()
            if history:
                history.store_message(msg)
                result["stored"] = True

            if self._ensure_comm():
                try:
                    report = self._comm.send(
                        recipient=recipient,
                        message=_pack_chat_payload(msg),
                        thread_id=thread_id,
                    )
                    if getattr(report, "delivered", False):
                        result["delivered"] = True
                        result["transport"] = getattr(report, "successful_transport", None)
                except Exception as exc:
                    result["error"] = str(exc)

        except ImportError as exc:
            result["error"] = f"Missing dependency: {exc}"

        return result

    def receive(self, limit: int = 20) -> list[dict]:
        """Poll for incoming messages.

        Args:
            limit: Maximum messages to return.

        Returns:
            list[dict]: Received message dicts.
        """
        messages: list[dict] = []

        if self._ensure_comm():
            try:
                envelopes = self._comm.receive()
                for env in envelopes:
                    if hasattr(env, "payload") and hasattr(env.payload, "content"):
                        msg_dict = _unpack_chat_payload(
                            env.payload.content,
                            sender=env.sender,
                            recipient=getattr(env, "recipient", self.identity),
                        )
                        messages.append(msg_dict)

                        history = self._ensure_history()
                        if history:
                            try:
                                from skchat.models import ChatMessage

                                chat_msg = ChatMessage(
                                    sender=msg_dict["sender"],
                                    recipient=msg_dict["recipient"],
                                    content=msg_dict["content"],
                                    thread_id=msg_dict.get("thread_id"),
                                )
                                history.store_message(chat_msg)
                            except Exception:
                                pass
            except Exception as exc:
                logger.warning("Receive error: %s", exc)

        return messages[:limit]

    def get_inbox(self, limit: int = 20) -> list[dict]:
        """Get recent messages from local history.

        Args:
            limit: Maximum messages to return.

        Returns:
            list[dict]: Message dicts from history.
        """
        history = self._ensure_history()
        if history is None:
            return []

        try:
            return history.search_messages(self.identity, limit=limit)
        except Exception:
            try:
                memories = history._store.list_memories(
                    tags=["skchat:message"],
                    limit=limit,
                )
                return [history._memory_to_chat_dict(m) for m in memories]
            except Exception:
                return []

    def live_session(
        self,
        peer: str,
        poll_interval: float = 2.0,
    ) -> None:
        """Run an interactive live chat session in the terminal.

        Alternates between polling for incoming messages and prompting
        for user input. Uses a simple blocking loop suitable for any
        terminal.

        Args:
            peer: Agent name or identity to chat with.
            poll_interval: Seconds between inbox polls.
        """
        thread_id = f"live-{self.identity}-{peer}-{int(time.time())}"

        print(f"\n  Sovereign Chat — {self.identity} <-> {peer}")
        print(f"  Thread: {thread_id[:20]}...")
        print(f"  Transport: {'available' if self._ensure_comm() else 'local-only'}")
        print(f"  Type a message and press Enter. Type /quit to exit.\n")

        try:
            while True:
                incoming = self.receive(limit=10)
                for msg in incoming:
                    sender = msg.get("sender", "?")
                    content = msg.get("content", "")
                    if sender != self.identity:
                        ts = _short_timestamp()
                        print(f"  [{ts}] {sender}: {content}")

                try:
                    user_input = _prompt_input(f"  [{_short_timestamp()}] {self.identity}: ")
                except EOFError:
                    break

                if not user_input:
                    continue
                if user_input.strip().lower() in ("/quit", "/exit", "/q"):
                    break

                result = self.send(peer, user_input, thread_id=thread_id)
                if result["delivered"]:
                    print(f"  [dim]  -> delivered via {result['transport']}[/]"
                          if False else f"    -> delivered via {result['transport']}")
                elif result["stored"]:
                    print(f"    -> stored locally")
                if result.get("error"):
                    print(f"    -> error: {result['error']}")

        except KeyboardInterrupt:
            pass

        print(f"\n  Session ended. {thread_id[:20]}...\n")


def _pack_chat_payload(msg) -> str:
    """Serialize a ChatMessage for SKComm transport.

    Args:
        msg: ChatMessage instance.

    Returns:
        str: JSON payload.
    """
    return json.dumps({
        "skchat_version": "1.0.0",
        "message_id": msg.id,
        "sender": msg.sender,
        "recipient": msg.recipient,
        "content": msg.content,
        "thread_id": msg.thread_id,
        "timestamp": msg.timestamp.isoformat(),
    })


def _unpack_chat_payload(payload: str, sender: str, recipient: str) -> dict:
    """Deserialize a chat payload from SKComm.

    Falls back to plain text if not structured JSON.

    Args:
        payload: Raw payload string.
        sender: Fallback sender from envelope.
        recipient: Fallback recipient from envelope.

    Returns:
        dict: Message data.
    """
    try:
        data = json.loads(payload)
        if "skchat_version" in data:
            return {
                "sender": data.get("sender", sender),
                "recipient": data.get("recipient", recipient),
                "content": data.get("content", payload),
                "thread_id": data.get("thread_id"),
                "timestamp": data.get("timestamp"),
            }
    except (json.JSONDecodeError, KeyError):
        pass

    return {
        "sender": sender,
        "recipient": recipient,
        "content": payload,
        "thread_id": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _short_timestamp() -> str:
    """Get a compact HH:MM:SS timestamp.

    Returns:
        str: Current time as HH:MM:SS.
    """
    return datetime.now().strftime("%H:%M:%S")


def _prompt_input(prompt: str) -> str:
    """Read a line of input from stdin with a prompt.

    Args:
        prompt: The prompt string to display.

    Returns:
        str: The user's input, stripped of trailing newline.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline().rstrip("\n")
