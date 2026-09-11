"""Mailbox verbs on the coord group: send, read, ack, tail, and bootstrap.

The transport is the Syncthing coordination folder, and the conflict-avoidance
rule lives in the FILENAME (one writer per host), not in a lock. See
``skcapstone.coord_mail`` for why.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from ..coord_mail import VALID_PRIORITIES, ack, bootstrap, read, send, tail
from ._common import AGENT_HOME, console


def _shown_sender(m: dict) -> str:
    """Render the sender qualified by estate when the record carries it.

    Two estates run the same agent names, so a bare "jarvis" in a report or a
    quoted message is ambiguous between two different beings. Show the fqid
    (`jarvis@chef.skworld`) whenever the record has one, and fall back to the
    bare name only for records that predate the stamp.
    """
    return str(m.get("from_fqid") or m.get("from"))


def register_coord_mail_commands(coord: click.Group) -> None:
    """Register mailbox + bootstrap verbs on the coord command group."""

    @coord.group("mail")
    def coord_mail() -> None:
        """Agent-to-agent mailbox over the coordination folder."""

    @coord_mail.command("send")
    @click.argument("to")
    @click.argument("body")
    @click.option("--from", "sender", required=True, help="Writer name (your agent).")
    @click.option(
        "--priority",
        type=click.Choice(VALID_PRIORITIES),
        default="normal",
        help="urgent means 'stop what you are doing'.",
    )
    @click.option("--re", "subject", default="", help="Subject line.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_mail_send(to, body, sender, priority, subject, home):
        """Append a message to your own writer file. Never writes a shared file."""
        rec = send(Path(home), sender, to, priority, subject, body)
        console.print(f"  sent to {rec['to']} [{rec['priority']}] re {rec['re']}", markup=False)

    @coord_mail.command("read")
    @click.argument("me")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--json", "as_json", is_flag=True, help="Emit raw JSON.")
    def coord_mail_read(me, home, as_json):
        """Unread messages addressed to you, oldest first. Does not advance the cursor."""
        msgs = read(Path(home), me)
        if as_json:
            click.echo(json.dumps(msgs, indent=2))
            return
        if not msgs:
            console.print(f"  no unread mail for [bold]{me}[/bold]")
            return
        for m in msgs:
            # markup=False: a priority like [urgent] is otherwise parsed as a
            # Rich style tag and silently disappears from the output.
            console.print(
                f"\n[{m.get('priority')}] {_shown_sender(m)} -> {m.get('to')}"
                f"  {str(m.get('ts',''))[:19]}",
                markup=False,
            )
            if m.get("re"):
                console.print(f"  re: {m['re']}")
            console.print(str(m.get("body", "")))

    @coord_mail.command("ack")
    @click.argument("me")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_mail_ack(me, home):
        """Mark everything currently visible to you as read."""
        n = ack(Path(home), me)
        console.print(f"  acked {n} message(s) for [bold]{me}[/bold]")

    @coord_mail.command("tail")
    @click.option("-n", "count", default=10, help="How many recent messages.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def coord_mail_tail(count, home):
        """Recent traffic between any peers."""
        for m in tail(Path(home), count):
            console.print(
                f"{str(m.get('ts',''))[:19]}  {_shown_sender(m)} -> {m.get('to')}"
                f"  [{m.get('priority')}] {str(m.get('re',''))[:50]}",
                markup=False,
            )

    @coord.command("bootstrap")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--agent", default=None, help="Also create this agent's own mailbox file.")
    @click.option(
        "--estate",
        default=None,
        help="Estate identifier. Resolved from cluster.json or the local host when omitted.",
    )
    @click.option(
        "--host",
        default=None,
        help="Host to elect for the six lifecycle seats. Defaults to this machine.",
    )
    def coord_bootstrap(home, agent, estate, host):
        """Create the coordination skeleton and the estate's seat plane.

        Idempotent and create-or-skip: re-running never overwrites a decision
        the estate has already recorded. Nothing else in the codebase creates
        any of this, which is why a fresh estate has no mailbox, no seat
        control plane and no node name until someone makes them by hand.

        Estate-wide items land in the synced tree and are shown relative to
        it. Host-local items land under XDG config and are shown as absolute
        paths, so the report itself shows which side of the split each one
        is on.
        """
        result = bootstrap(Path(home), agent, host=host, estate=estate)
        console.print(
            f"  estate [bold]{result['estate']}[/bold]"
            f"  host [bold]{result['host']}[/bold]"
            f"  node [bold]{result['node']}[/bold]"
        )
        if result["created"]:
            console.print(f"  created for [bold]{result['home']}[/bold]:")
            for c in result["created"]:
                console.print(f"    + {c}", markup=False)
        else:
            console.print(f"  nothing to create; [bold]{result['home']}[/bold] already complete")
        if result["mailbox"]:
            console.print(f"  mailbox: {result['mailbox']}")
