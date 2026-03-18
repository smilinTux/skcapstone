"""
Interactive agent-to-agent chat for the sovereign terminal.

Provides a real-time terminal chat experience between agents using
SKChat for message models and SKComm for transport. Works from any
terminal on any platform — no IDE dependency.

Usage:
    skcapstone chat <peer>           # interactive session (prompt_toolkit)
    skcapstone chat send <peer> <m>  # one-shot send
    skcapstone chat inbox            # browse messages
    skcapstone chat live <peer>      # alias for interactive session
"""

from __future__ import annotations

import base64
import json
import logging
import threading as _threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.chat")

# Slash commands available in interactive mode
_CHAT_COMMANDS = [
    "/attach",
    "/emoji",
    "/exit",
    "/help",
    "/inbox",
    "/q",
    "/quit",
    "/reply",
    "/thread",
    "/whoami",
]

# Text file extensions that can be sent as UTF-8 (not base64)
_TEXT_SUFFIXES = {
    ".bash", ".cfg", ".conf", ".css", ".csv", ".env",
    ".go", ".html", ".js", ".json", ".log", ".md",
    ".py", ".rs", ".sh", ".toml", ".ts", ".txt",
    ".xml", ".yaml", ".yml", ".zsh",
}


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

    # ------------------------------------------------------------------
    # Transport / history bootstrap
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Core messaging API
    # ------------------------------------------------------------------

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
                            except Exception as exc:
                                logger.warning("Failed to store received message in history: %s", exc)
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

    def forward(
        self,
        original_msg: dict,
        target_peer: str,
        thread_id: Optional[str] = None,
    ) -> dict:
        """Forward a message to another peer, preserving original sender/timestamp.

        Wraps the original message in a forward envelope that records the
        original sender and timestamp, then delivers it to target_peer via
        SKComm and stores it locally in history.

        Args:
            original_msg: Original message dict (from inbox or receive).
            target_peer: Peer agent to forward the message to.
            thread_id: Optional thread ID for the forwarded message.

        Returns:
            dict: Result with 'stored', 'delivered', 'transport', 'forwarded_id' keys.
        """
        result: dict = {
            "stored": False,
            "delivered": False,
            "transport": None,
            "forwarded_id": None,
            "error": None,
        }

        fwd_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        payload = json.dumps({
            "skchat_version": "1.0.0",
            "skchat_forward": True,
            "message_id": fwd_id,
            "sender": self.identity,
            "recipient": target_peer,
            "content": original_msg.get("content", ""),
            "thread_id": thread_id,
            "timestamp": now,
            "forwarded_from": original_msg.get("sender", "unknown"),
            "forwarded_at": original_msg.get("timestamp", ""),
            "original_message_id": original_msg.get("message_id", ""),
        })

        result["forwarded_id"] = fwd_id

        history = self._ensure_history()
        if history:
            try:
                from skchat.models import ChatMessage, DeliveryStatus

                fwd_msg = ChatMessage(
                    sender=self.identity,
                    recipient=target_peer,
                    content=payload,
                    thread_id=thread_id,
                    delivery_status=DeliveryStatus.PENDING,
                )
                history.store_message(fwd_msg)
                result["stored"] = True
            except Exception as exc:
                result["error"] = str(exc)

        if self._ensure_comm():
            try:
                report = self._comm.send(
                    recipient=target_peer,
                    message=payload,
                    thread_id=thread_id,
                )
                if getattr(report, "delivered", False):
                    result["delivered"] = True
                    result["transport"] = getattr(report, "successful_transport", None)
            except Exception as exc:
                result["error"] = str(exc)

        return result

    # ------------------------------------------------------------------
    # Interactive sessions
    # ------------------------------------------------------------------

    def interactive_session(
        self,
        peer: str,
        poll_interval: float = 2.0,
        thread_id: Optional[str] = None,
    ) -> None:
        """Run a prompt_toolkit-powered interactive chat session.

        Features:
        - Rich input with command history, auto-suggest, tab completion
        - Bottom toolbar showing peer, active thread, transport status
        - Background thread polls for incoming messages (non-blocking)
        - File attachments via /attach <path>
        - Thread management via /thread <id> and /reply
        - Emoji support — just type unicode directly

        Falls back to live_session() if prompt_toolkit is not installed.

        Args:
            peer: Agent name or CapAuth identity to chat with.
            poll_interval: Seconds between incoming message polls.
            thread_id: Optional starting thread ID.
        """
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.formatted_text import HTML
            from prompt_toolkit.history import InMemoryHistory
            from prompt_toolkit.patch_stdout import patch_stdout
            from prompt_toolkit.styles import Style
        except ImportError:
            logger.info("prompt_toolkit not installed — falling back to live_session")
            self.live_session(peer, poll_interval=poll_interval)
            return

        # Mutable state shared with background thread
        state: dict = {
            "thread": thread_id or f"chat-{self.identity}-{peer}-{int(time.time())}",
            "last_recv_thread": None,
        }
        seen_ids: set[str] = set()
        transport_ok = self._ensure_comm()

        # prompt_toolkit style
        style = Style.from_dict({
            "bottom-toolbar": "bg:#1a1a2e #aaaaaa",
        })

        def bottom_toolbar() -> HTML:
            conn = "connected" if self._ensure_comm() else "local-only"
            t = state["thread"]
            short_t = (t[:20] + "…") if len(t) > 20 else t
            return HTML(
                f" <b>Sovereign Chat</b>  "
                f"peer: <ansicyan>{peer}</ansicyan>  "
                f"thread: <ansigreen>{short_t}</ansigreen>  "
                f"[{conn}]"
            )

        session = PromptSession(
            history=InMemoryHistory(),
            auto_suggest=AutoSuggestFromHistory(),
            completer=WordCompleter(_CHAT_COMMANDS, sentence=True, ignore_case=True),
            style=style,
            bottom_toolbar=bottom_toolbar,
            mouse_support=False,
        )

        # Background polling thread — prints via patch_stdout
        stop_event = _threading.Event()

        def _poll_loop() -> None:
            while not stop_event.wait(poll_interval):
                try:
                    for msg in self.receive(limit=20):
                        uid = msg.get("message_id") or msg.get("id") or ""
                        if uid and uid in seen_ids:
                            continue
                        if uid:
                            seen_ids.add(uid)
                        sender = msg.get("sender", "?")
                        if sender == self.identity:
                            continue
                        content = msg.get("content", "")
                        ts = _short_timestamp()
                        recv_thread = msg.get("thread_id")
                        if recv_thread:
                            state["last_recv_thread"] = recv_thread
                        display = _format_content(content)
                        print(f"\n  \033[32m{sender}\033[0m \033[2m[{ts}]\033[0m  {display}\n")
                except Exception as exc:
                    logger.warning("Chat poll loop error: %s", exc)

        # Print header
        tr_label = "✓ connected" if transport_ok else "✗ local-only"
        t = state["thread"]
        short_t = (t[:30] + "…") if len(t) > 30 else t
        print(f"\n  ─── Sovereign Chat ─────────────────────────────")
        print(f"  Peer:      {peer}")
        print(f"  Thread:    {short_t}")
        print(f"  Transport: {tr_label}")
        print(f"  ────────────────────────────────────────────────")
        print(f"  Type a message and press Enter to send.")
        print(f"  /attach <path>  /thread <id>  /reply  /help  /quit\n")

        with patch_stdout():
            poll_thread = _threading.Thread(target=_poll_loop, daemon=True)
            poll_thread.start()

            while True:
                try:
                    text = session.prompt(f"  {self.identity}: ")
                except (EOFError, KeyboardInterrupt):
                    break

                text = text.strip()
                if not text:
                    continue

                low = text.lower()

                # ── Slash commands ──────────────────────────────────
                if low in ("/quit", "/exit", "/q"):
                    break

                elif low == "/help":
                    _print_chat_help()

                elif low == "/whoami":
                    print(f"\n  Identity: {self.identity}\n")

                elif low == "/inbox":
                    _print_recent_inbox(self.get_inbox(limit=5))

                elif low == "/reply":
                    lt = state.get("last_recv_thread")
                    if lt:
                        state["thread"] = lt
                        print(f"\n  Now replying in thread: {lt}\n")
                    else:
                        print("\n  No received thread to reply to yet.\n")

                elif low.startswith("/thread "):
                    new_t = text[8:].strip()
                    if new_t:
                        state["thread"] = new_t
                        print(f"\n  Switched to thread: {new_t}\n")

                elif low.startswith("/attach "):
                    fp = text[8:].strip()
                    self._send_attachment(peer, fp, state["thread"])

                elif low == "/emoji":
                    _print_emoji_ref()

                else:
                    # Regular message send
                    result = self.send(peer, text, thread_id=state["thread"])
                    ts = _short_timestamp()
                    if result.get("delivered"):
                        via = result.get("transport") or "unknown"
                        print(
                            f"  \033[34m{self.identity}\033[0m \033[2m[{ts}]\033[0m"
                            f"  {text}  \033[2m→ {via}\033[0m"
                        )
                    elif result.get("stored"):
                        print(
                            f"  \033[34m{self.identity}\033[0m \033[2m[{ts}]\033[0m"
                            f"  {text}  \033[2m→ stored locally\033[0m"
                        )
                    else:
                        err = result.get("error") or "send failed"
                        print(f"\n  \033[31mError:\033[0m {err}\n")

        stop_event.set()
        print(f"\n  Chat session ended.\n")

    def live_session(
        self,
        peer: str,
        poll_interval: float = 2.0,
    ) -> None:
        """Run an interactive live chat session in the terminal.

        Fallback implementation using plain stdin/stdout. Used when
        prompt_toolkit is not installed.

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
                    user_input = _read_line(f"  [{_short_timestamp()}] {self.identity}: ")
                except EOFError:
                    break

                if not user_input:
                    continue
                if user_input.strip().lower() in ("/quit", "/exit", "/q"):
                    break

                result = self.send(peer, user_input, thread_id=thread_id)
                if result["delivered"]:
                    print(f"    -> delivered via {result['transport']}")
                elif result["stored"]:
                    print(f"    -> stored locally")
                if result.get("error"):
                    print(f"    -> error: {result['error']}")

        except KeyboardInterrupt:
            pass

        print(f"\n  Session ended.\n")

    # ------------------------------------------------------------------
    # File attachment
    # ------------------------------------------------------------------

    def _send_attachment(self, peer: str, file_path_str: str, thread_id: str) -> None:
        """Send a file as an inline attachment message.

        Reads the file, encodes it (UTF-8 for text, base64 for binary),
        and sends it as a structured JSON payload. The recipient sees
        a [Attachment: name (size)] preview.

        Args:
            peer: Recipient agent name.
            file_path_str: Path to the file to send.
            thread_id: Active thread ID.
        """
        path = Path(file_path_str).expanduser().resolve()
        if not path.exists():
            print(f"\n  File not found: {path}\n")
            return
        if not path.is_file():
            print(f"\n  Not a regular file: {path}\n")
            return

        size = path.stat().st_size
        if size > 10 * 1024 * 1024:  # 10 MB cap for inline transfers
            print(
                f"\n  File too large: {size:,} bytes (max 10 MB for inline attachments).\n"
                f"  Use skcapstone file send for large transfers.\n"
            )
            return

        raw = path.read_bytes()

        if path.suffix.lower() in _TEXT_SUFFIXES:
            try:
                payload = json.dumps({
                    "skchat_attachment": True,
                    "name": path.name,
                    "size": size,
                    "encoding": "utf-8",
                    "content": raw.decode("utf-8"),
                })
            except UnicodeDecodeError:
                payload = json.dumps({
                    "skchat_attachment": True,
                    "name": path.name,
                    "size": size,
                    "encoding": "base64",
                    "content": base64.b64encode(raw).decode(),
                })
        else:
            payload = json.dumps({
                "skchat_attachment": True,
                "name": path.name,
                "size": size,
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            })

        result = self.send(peer, payload, thread_id=thread_id)
        ts = _short_timestamp()
        display = f"[Attachment: {path.name} ({size:,} bytes)]"
        if result.get("delivered") or result.get("stored"):
            via = result.get("transport") or "stored"
            print(
                f"  \033[34m{self.identity}\033[0m \033[2m[{ts}]\033[0m"
                f"  {display}  \033[2m→ {via}\033[0m"
            )
        else:
            print(f"\n  Failed to send attachment: {result.get('error', 'unknown')}\n")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

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
                "message_id": data.get("message_id", ""),
                "sender": data.get("sender", sender),
                "recipient": data.get("recipient", recipient),
                "content": data.get("content", payload),
                "thread_id": data.get("thread_id"),
                "timestamp": data.get("timestamp"),
            }
    except (json.JSONDecodeError, KeyError):
        pass

    return {
        "message_id": "",
        "sender": sender,
        "recipient": recipient,
        "content": payload,
        "thread_id": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _format_content(content: str) -> str:
    """Format a message content string for terminal display.

    Detects inline attachments and renders them as a readable label.

    Args:
        content: Raw message content.

    Returns:
        str: Display-ready string.
    """
    try:
        data = json.loads(content)
        if data.get("skchat_attachment"):
            name = data.get("name", "unknown")
            size = data.get("size", 0)
            return f"[Attachment: {name} ({size:,} bytes)]"
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return content


def _short_timestamp() -> str:
    """Get a compact HH:MM:SS timestamp."""
    return datetime.now().strftime("%H:%M:%S")


def _read_line(prompt: str) -> str:
    """Read a line of input from stdin with a prompt.

    Args:
        prompt: The prompt string to display.

    Returns:
        str: The user's input, stripped of trailing newline.
    """
    import sys
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return sys.stdin.readline().rstrip("\n")


def _print_chat_help() -> None:
    """Print the chat command reference."""
    print(
        "\n  Chat commands:\n"
        "    /attach <path>   Send a file attachment (max 10 MB)\n"
        "    /thread <id>     Switch to a different thread ID\n"
        "    /reply           Switch to the last received thread\n"
        "    /inbox           Show last 5 inbox messages\n"
        "    /whoami          Show your agent identity\n"
        "    /emoji           Emoji quick reference\n"
        "    /quit            Exit  (also /exit or /q)\n"
        "\n  Emoji is fully supported — just type directly: 🎉 🚀 ❤️ 🤖\n"
    )


def _print_recent_inbox(messages: list) -> None:
    """Print a short inbox preview."""
    if not messages:
        print("\n  No messages in inbox.\n")
        return
    print("\n  Recent messages:")
    for m in messages:
        sender = m.get("sender", "?")
        content = m.get("content", "")
        display = _format_content(content)
        preview = (display[:60] + "…") if len(display) > 60 else display
        print(f"    \033[36m{sender}\033[0m: {preview}")
    print()


def _print_emoji_ref() -> None:
    """Print an emoji quick-reference card."""
    print(
        "\n  Emoji quick reference (type directly — unicode is fully supported):\n"
        "    ❤️  💙  💚  🖤  🤍   — hearts\n"
        "    👍  👎  🙌  🤝  ✌️   — hands\n"
        "    🎉  🚀  🔥  ⚡  ✨   — vibes\n"
        "    ✅  ❌  ⚠️  🔒  🔑   — status\n"
        "    🤖  👾  🧠  🔮  💡   — tech / sovereign\n"
    )
