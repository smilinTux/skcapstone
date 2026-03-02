"""Peer directory commands: peers list, peers add, peers remove, peers discover."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.table import Table


def register_peers_dir_commands(main: click.Group) -> None:
    """Register the peers (directory) command group."""

    @main.group("peers")
    def peers_dir():
        """Peer transport directory — routing addresses for the mesh."""

    @peers_dir.command("list")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def peers_list(sk_home, json_out):
        """List all peers in the transport directory."""
        from ..peer_directory import PeerDirectory

        directory = PeerDirectory(home=Path(sk_home).expanduser())
        peers = directory.list_peers()

        if json_out:
            click.echo(json.dumps([p.model_dump() for p in peers], indent=2, default=str))
            return

        console.print()
        if not peers:
            console.print("  [dim]No peers in directory.[/]")
            console.print("  Add one: skcapstone peers add --name lumina --address /path/to/outbox/lumina")
            console.print("  Or auto-discover: skcapstone peers discover")
            console.print()
            return

        table = Table(
            show_header=True,
            header_style="bold",
            box=None,
            padding=(0, 2),
            title=f"Peer Transport Directory ({len(peers)})",
        )
        table.add_column("Name", style="cyan")
        table.add_column("Transport", style="dim")
        table.add_column("Address")
        table.add_column("Last Seen", style="dim")

        for p in peers:
            last = p.last_seen[:19] if p.last_seen else "[dim]never[/]"
            table.add_row(p.name, p.transport, p.address, last)

        console.print(table)
        console.print()

    @peers_dir.command("add")
    @click.option("--name", required=True, help="Peer name.")
    @click.option("--address", required=True, help="Transport address (path, IP, URI).")
    @click.option(
        "--transport",
        default="syncthing",
        show_default=True,
        help="Transport type: syncthing, webrtc, tailscale, file.",
    )
    @click.option("--fingerprint", default="", help="PGP fingerprint (optional).")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    def peers_add(name, address, transport, fingerprint, sk_home):
        """Add a peer to the transport directory."""
        from ..peer_directory import PeerDirectory

        directory = PeerDirectory(home=Path(sk_home).expanduser())
        entry = directory.add_peer(
            name=name,
            address=address,
            transport=transport,
            fingerprint=fingerprint,
        )
        console.print(f"\n  [green]Added peer:[/] [cyan]{entry.name}[/]")
        console.print(f"  Transport: {entry.transport}")
        console.print(f"  Address:   {entry.address}")
        if entry.fingerprint:
            console.print(f"  Fingerprint: [dim]{entry.fingerprint[:20]}...[/]")
        console.print()

    @peers_dir.command("remove")
    @click.argument("name")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    def peers_remove(name, sk_home):
        """Remove a peer from the transport directory."""
        from ..peer_directory import PeerDirectory

        directory = PeerDirectory(home=Path(sk_home).expanduser())
        removed = directory.remove_peer(name)
        if removed:
            console.print(f"\n  [green]Removed:[/] {name}\n")
        else:
            console.print(f"\n  [yellow]Peer '{name}' not in directory.[/]\n")

    @peers_dir.command("discover")
    @click.option("--home", "sk_home", default=AGENT_HOME, type=click.Path())
    def peers_discover(sk_home):
        """Auto-discover peers from heartbeat files and Syncthing outbox."""
        from ..peer_directory import PeerDirectory

        directory = PeerDirectory(home=Path(sk_home).expanduser())
        added = directory.auto_discover()

        console.print()
        if not added:
            console.print("  [dim]No new peers discovered.[/]")
        else:
            console.print(f"  [green]Discovered {len(added)} new peer(s):[/]")
            for entry in added:
                console.print(f"    [cyan]{entry.name}[/] — {entry.transport} → {entry.address}")
        console.print()
