"""test-connection command — ping a peer via SKComm and measure latency.

Usage:
    skcapstone test-connection <peer>
    skcapstone test-connection <peer> --timeout 5
    skcapstone test-connection <peer> --count 3
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import click

from ._common import AGENT_HOME, console
from ..chat import AgentChat


# Payload type markers
_PING_KEY = "skchat_ping"
_PONG_KEY = "skchat_pong"


def _make_ping_payload(nonce: str, sender: str) -> str:
    """Serialize a ping message.

    Args:
        nonce: Unique identifier for this ping/pong round-trip.
        sender: Sending agent name.

    Returns:
        str: JSON payload string.
    """
    return json.dumps({_PING_KEY: True, "nonce": nonce, "sender": sender})


def _is_pong_for(payload: str, nonce: str) -> bool:
    """Return True if *payload* is a pong matching *nonce*.

    Args:
        payload: Raw message content string.
        nonce: The nonce sent in the corresponding ping.

    Returns:
        bool: True when the payload is a valid matching pong.
    """
    try:
        data = json.loads(payload)
        return bool(data.get(_PONG_KEY)) and data.get("nonce") == nonce
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False


def _is_ping(payload: str) -> tuple[bool, str, str]:
    """Parse an incoming ping payload.

    Args:
        payload: Raw message content string.

    Returns:
        tuple: (is_ping, nonce, sender) — nonce/sender are empty strings
        when the payload is not a ping.
    """
    try:
        data = json.loads(payload)
        if data.get(_PING_KEY):
            return True, data.get("nonce", ""), data.get("sender", "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return False, "", ""


def _make_pong_payload(nonce: str, sender: str) -> str:
    """Serialize a pong reply.

    Args:
        nonce: The nonce received from the ping.
        sender: Replying agent name.

    Returns:
        str: JSON payload string.
    """
    return json.dumps({_PONG_KEY: True, "nonce": nonce, "sender": sender})


def ping_peer(
    peer: str,
    home: Path,
    identity: str,
    timeout: float = 10.0,
) -> dict:
    """Send a ping to *peer* and wait for a matching pong.

    Sends a structured ping message via AgentChat and polls the inbox
    for a pong response with a matching nonce. Times out after *timeout*
    seconds.

    Args:
        peer: Peer agent name or identity.
        home: Agent home directory.
        identity: Local agent name used as sender.
        timeout: Maximum seconds to wait for a pong.

    Returns:
        dict with keys:
            - ``reachable`` (bool): True if pong was received in time.
            - ``latency_ms`` (float | None): Round-trip time in milliseconds,
              or None on timeout.
            - ``transport`` (str | None): Transport that delivered the ping.
            - ``error`` (str | None): Error message, if any.
    """
    result: dict = {
        "reachable": False,
        "latency_ms": None,
        "transport": None,
        "error": None,
    }

    nonce = str(uuid.uuid4())
    agent_chat = AgentChat(home=home, identity=identity)

    # Send the ping
    t0 = time.monotonic()
    send_result = agent_chat.send(peer, _make_ping_payload(nonce, identity))

    if not send_result.get("delivered") and not send_result.get("stored"):
        result["error"] = send_result.get("error") or "send failed (no transport)"
        return result

    result["transport"] = send_result.get("transport")

    # If we only stored locally (no live transport), report accordingly
    if not send_result.get("delivered"):
        result["error"] = "ping stored locally — no live transport available"
        return result

    # Poll for pong
    deadline = t0 + timeout
    poll_interval = 0.25  # seconds

    while time.monotonic() < deadline:
        messages = agent_chat.receive(limit=20)
        for msg in messages:
            content = msg.get("content", "")
            sender_name = msg.get("sender", "")
            if sender_name == peer and _is_pong_for(content, nonce):
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                result["reachable"] = True
                result["latency_ms"] = round(elapsed_ms, 2)
                return result
        time.sleep(poll_interval)

    result["error"] = f"timeout after {timeout:.0f}s — no pong received"
    return result


def register_test_connection_commands(main: click.Group) -> None:
    """Register the test-connection command on *main*."""

    @main.command("test-connection")
    @click.argument("peer")
    @click.option(
        "--timeout", "-t",
        default=10.0,
        show_default=True,
        help="Seconds to wait for a pong response.",
    )
    @click.option(
        "--count", "-c",
        default=1,
        show_default=True,
        help="Number of pings to send (reports min/avg/max when > 1).",
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def test_connection(peer: str, timeout: float, count: int, home: str) -> None:
        """Test connectivity to a peer by sending a ping via SKComm.

        Sends a ping message, waits for the peer to reply with a pong,
        and reports the round-trip latency. Exits with code 1 when the
        peer is unreachable.

        \b
        Examples:
          skcapstone test-connection lumina
          skcapstone test-connection opus --timeout 5
          skcapstone test-connection jarvis --count 3
        """
        from ..runtime import get_runtime

        home_path = Path(home).expanduser()
        try:
            runtime = get_runtime(home_path)
            identity = runtime.manifest.name or "unknown"
        except Exception:
            identity = "unknown"

        console.print()
        console.print(
            f"  PING [cyan]{peer}[/]  (timeout={timeout:.0f}s, count={count})"
        )
        console.print()

        latencies: list[float] = []
        last_error: str | None = None
        last_transport: str | None = None

        for i in range(count):
            result = ping_peer(peer, home_path, identity, timeout=timeout)
            last_transport = result.get("transport") or last_transport

            if result["reachable"]:
                ms = result["latency_ms"]
                latencies.append(ms)
                console.print(
                    f"  [{i + 1}/{count}]  [green]pong from {peer}[/]  "
                    f"latency=[bold]{ms:.1f} ms[/]"
                )
            else:
                last_error = result.get("error") or "unreachable"
                console.print(
                    f"  [{i + 1}/{count}]  [red]no pong[/]  {last_error}"
                )

        console.print()

        if latencies:
            if count > 1:
                lo = min(latencies)
                hi = max(latencies)
                avg = sum(latencies) / len(latencies)
                console.print(
                    f"  [bold green]REACHABLE[/]  "
                    f"min={lo:.1f}ms  avg={avg:.1f}ms  max={hi:.1f}ms  "
                    f"({len(latencies)}/{count} received)"
                )
            else:
                console.print(
                    f"  [bold green]REACHABLE[/]  "
                    f"latency={latencies[0]:.1f} ms"
                )
            if last_transport:
                console.print(f"  transport: [dim]{last_transport}[/]")
        else:
            console.print(
                f"  [bold red]UNREACHABLE[/]  {last_error or 'no pong received'}"
            )
            console.print()
            raise SystemExit(1)

        console.print()
