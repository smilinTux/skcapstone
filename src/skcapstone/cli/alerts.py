"""Alerts command — subscribe to critical pubsub topics and stream live alerts."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.panel import Panel
from rich.text import Text

from ._common import AGENT_HOME, console
from .. import SKCAPSTONE_AGENT, SKCAPSTONE_ROOT

# Import NotificationManager at module level so it can be patched in tests.
try:
    from ..notifications import NotificationManager
except ImportError:  # pragma: no cover
    NotificationManager = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default topics the alerts command subscribes to.
DEFAULT_TOPICS: tuple[str, ...] = (
    "agent.critical",
    "coord.task_failed",
    "consciousness.error",
    "pillar.degraded",
)

#: Rich markup styles per topic prefix (longest match wins).
_TOPIC_STYLE: dict[str, str] = {
    "agent.critical":     "bold red",
    "coord.task_failed":  "red",
    "consciousness.error": "bold magenta",
    "pillar.degraded":    "yellow",
}

#: Default polling interval in seconds.
DEFAULT_INTERVAL: float = 1.0


# ---------------------------------------------------------------------------
# Helpers (importable for unit-testing)
# ---------------------------------------------------------------------------

def _style_for_topic(topic: str) -> str:
    """Return the Rich markup style for a given topic name.

    Args:
        topic: Full topic name (e.g. ``"agent.critical"``).

    Returns:
        Rich style string for the topic, or ``"dim"`` if unrecognised.
    """
    return _TOPIC_STYLE.get(topic, "dim")


def _format_payload(payload: dict) -> str:
    """Render a message payload as a pretty-printed JSON string.

    Args:
        payload: Arbitrary message payload dict.

    Returns:
        Indented JSON string (2-space indent).
    """
    return json.dumps(payload, indent=2, default=str)


def _make_alert_panel(msg) -> Panel:
    """Build a Rich Panel for a single TopicMessage.

    Args:
        msg: A ``TopicMessage`` instance from the pub/sub bus.

    Returns:
        A Rich ``Panel`` ready for ``console.print()``.
    """
    style = _style_for_topic(msg.topic)
    ts = msg.published_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    body = Text()
    body.append("sender:  ", style="bold")
    body.append(f"{msg.sender}\n")
    body.append("time:    ", style="bold")
    body.append(f"{ts}\n")
    body.append("id:      ", style="bold")
    body.append(f"{msg.message_id}\n")
    if msg.payload:
        body.append("payload:\n", style="bold")
        body.append(_format_payload(msg.payload))

    title = Text()
    title.append(" ALERT ", style=f"{style} reverse")
    title.append("  ")
    title.append(msg.topic, style=style)

    return Panel(body, title=title, border_style=style)


def _resolve_home(agent: str | None, home: str) -> Path:
    """Resolve the agent home directory.

    Args:
        agent: Named agent (e.g. ``"opus"``), or None for default.
        home:  Fallback home directory string.

    Returns:
        Resolved ``Path`` to the agent home.
    """
    if agent:
        return (Path(SKCAPSTONE_ROOT) / "agents" / agent).expanduser()
    return Path(home).expanduser()


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def register_alerts_commands(main: click.Group) -> None:
    """Register the top-level ``skcapstone alerts`` command."""

    @main.command("alerts")
    @click.option(
        "--home", default=AGENT_HOME, type=click.Path(),
        help="Agent home directory.",
    )
    @click.option(
        "--agent", default=None,
        help="Named agent whose pubsub to monitor (e.g. opus, jarvis).",
    )
    @click.option(
        "--notify", is_flag=True,
        help="Fire a desktop notification for each alert received.",
    )
    @click.option(
        "--interval", default=DEFAULT_INTERVAL, show_default=True,
        type=float,
        help="Polling interval in seconds.",
    )
    @click.option(
        "--once", is_flag=True,
        help="Poll once and exit (instead of continuous streaming).",
    )
    @click.option(
        "--topic", "extra_topics", multiple=True,
        help="Additional topic to subscribe to (repeatable).",
    )
    def alerts_command(
        home: str,
        agent: Optional[str],
        notify: bool,
        interval: float,
        once: bool,
        extra_topics: tuple[str, ...],
    ) -> None:
        """Stream live alerts from critical pubsub topics.

        Subscribes to agent.critical, coord.task_failed, consciousness.error,
        and pillar.degraded, then prints incoming messages with rich formatting.

        Use --notify to also fire a desktop notification for each alert.

        Examples:

            skcapstone alerts

            skcapstone alerts --notify

            skcapstone alerts --once

            skcapstone alerts --topic my.custom.topic

            skcapstone alerts --interval 0.5
        """
        from ..pubsub import PubSub

        agent_name = agent or SKCAPSTONE_AGENT or "anonymous"
        agent_home = _resolve_home(agent, home)

        topics = list(DEFAULT_TOPICS) + list(extra_topics)

        bus = PubSub(agent_home, agent_name=agent_name)
        bus.initialize()

        for topic in topics:
            bus.subscribe(topic)

        notifier = None
        if notify:
            if NotificationManager is None:
                console.print("[yellow]Warning: desktop notifications unavailable.[/]")
            else:
                try:
                    notifier = NotificationManager()
                except Exception:
                    console.print("[yellow]Warning: desktop notifications unavailable.[/]")

        console.print(
            f"[bold]Monitoring {len(topics)} topics[/]  "
            f"([dim]{', '.join(topics)}[/])\n"
            "[dim]Press Ctrl-C to stop.[/]\n"
        )

        since: Optional[datetime] = None
        total = 0

        try:
            while True:
                messages = bus.poll(since=since)
                # poll() returns newest-first; reverse so we print oldest first
                for msg in reversed(messages):
                    if msg.topic not in topics:
                        continue
                    panel = _make_alert_panel(msg)
                    console.print(panel)
                    total += 1

                    if notifier is not None:
                        style_label = msg.topic.upper()
                        body_preview = json.dumps(msg.payload, default=str)[:120]
                        notifier.notify(
                            title=f"SKCapstone Alert: {style_label}",
                            body=f"From {msg.sender}: {body_preview}",
                            urgency="critical",
                        )

                if messages:
                    # Advance since to avoid re-showing the same messages
                    since = max(m.published_at for m in messages)

                if once:
                    if total == 0:
                        console.print("[dim]No alerts found.[/]")
                    break

                time.sleep(interval)

        except KeyboardInterrupt:
            console.print(
                f"\n[dim]Stopped. {total} alert(s) received.[/]"
            )
