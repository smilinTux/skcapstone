"""Agent card commands: generate, show, verify, export."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ._common import AGENT_HOME, console
from ..runtime import get_runtime

from rich.panel import Panel


def register_card_commands(main: click.Group) -> None:
    """Register the card command group."""

    @main.group()
    def card():
        """Agent card — shareable sovereign identity for P2P discovery.

        Generate, view, export, and verify sovereign agent identity cards.
        Cards contain your CapAuth identity, contact transports, and capabilities.
        """

    @card.command("generate")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--capauth-home", default="~/.capauth", type=click.Path(), help="CapAuth home.")
    @click.option("--motto", default=None, help="Short tagline for the card.")
    @click.option("--output", "-o", default=None, type=click.Path(), help="Output file path.")
    @click.option("--sign", "do_sign", is_flag=True, default=False, help="Sign the card with your PGP key.")
    @click.option("--passphrase", "-p", default=None, hide_input=True, help="PGP passphrase for signing.")
    def card_generate(home, capauth_home, motto, output, do_sign, passphrase):
        """Generate an agent card from your CapAuth profile."""
        from ..agent_card import AgentCapability, AgentCard

        home_path = Path(home).expanduser()

        try:
            agent_card = AgentCard.from_capauth_profile(
                profile_dir=capauth_home,
                capabilities=[
                    AgentCapability(name="chat", description="SKChat encrypted messaging"),
                    AgentCapability(name="memory", description="SKMemory persistent context"),
                ],
            )
        except FileNotFoundError:
            runtime = get_runtime(home_path)
            m = runtime.manifest
            agent_card = AgentCard.generate(
                name=m.name, fingerprint=m.identity.fingerprint or "unknown",
                public_key="", entity_type="ai",
            )

        if motto:
            agent_card.motto = motto

        if do_sign:
            if not passphrase:
                passphrase = click.prompt("PGP passphrase", hide_input=True)
            capauth_path = Path(capauth_home).expanduser()
            priv_path = capauth_path / "identity" / "private.asc"
            if priv_path.exists():
                agent_card.sign(priv_path.read_text(encoding="utf-8"), passphrase)
                console.print("[green]Card signed.[/]")
            else:
                console.print("[yellow]Private key not found, card unsigned.[/]")

        out_path = output or str(home_path / "agent-card.json")
        agent_card.save(out_path)

        console.print(Panel(agent_card.summary(), title="Agent Card Generated", border_style="cyan"))
        console.print(f"  [dim]Saved to: {out_path}[/]\n")

    @card.command("show")
    @click.argument("filepath", default="~/.skcapstone/agent-card.json")
    def card_show(filepath):
        """Display an agent card."""
        from ..agent_card import AgentCard

        try:
            agent_card = AgentCard.load(filepath)
        except FileNotFoundError:
            console.print(f"[red]Card not found: {filepath}[/]")
            raise SystemExit(1)

        verified = AgentCard.verify_signature(agent_card)
        sig_str = "[green]VALID[/]" if verified else (
            "[yellow]unsigned[/]" if not agent_card.signature else "[red]INVALID[/]"
        )

        console.print(Panel(
            f"[bold]{agent_card.name}[/] ({agent_card.entity_type})\n"
            f"Fingerprint: [cyan]{agent_card.fingerprint[:16]}...[/]\n"
            f"Trust: depth={agent_card.trust_depth} entangled={agent_card.entangled}\n"
            f"Signature: {sig_str}\n"
            f"Transports: {len(agent_card.transports)}\n"
            f"Capabilities: {', '.join(c.name for c in agent_card.capabilities) or 'none'}\n"
            + (f'Motto: "{agent_card.motto}"' if agent_card.motto else ""),
            title=f"Agent Card: {agent_card.name}",
            border_style="cyan",
        ))

    @card.command("verify")
    @click.argument("filepath")
    def card_verify(filepath):
        """Verify the PGP signature on an agent card."""
        from ..agent_card import AgentCard

        try:
            agent_card = AgentCard.load(filepath)
        except FileNotFoundError:
            console.print(f"[red]Card not found: {filepath}[/]")
            raise SystemExit(1)

        if not agent_card.signature:
            console.print("[yellow]Card is not signed.[/]")
            raise SystemExit(1)

        if AgentCard.verify_signature(agent_card):
            console.print(Panel(
                f"[bold green]VERIFIED[/]\nAgent: {agent_card.name}\n"
                f"Fingerprint: {agent_card.fingerprint[:16]}...",
                title="Signature Valid", border_style="green",
            ))
        else:
            console.print(Panel(
                f"[bold red]SIGNATURE INVALID[/]\nAgent: {agent_card.name}\n"
                "The card may have been tampered with.",
                title="Verification Failed", border_style="red",
            ))
            raise SystemExit(1)

    @card.command("export")
    @click.argument("filepath", default="~/.skcapstone/agent-card.json")
    @click.option("--compact", is_flag=True, help="Export compact format (no public key).")
    def card_export(filepath, compact):
        """Export an agent card to stdout (for sharing)."""
        from ..agent_card import AgentCard

        try:
            agent_card = AgentCard.load(filepath)
        except FileNotFoundError:
            console.print(f"[red]Card not found: {filepath}[/]")
            raise SystemExit(1)

        if compact:
            click.echo(json.dumps(agent_card.to_compact(), indent=2))
        else:
            click.echo(agent_card.model_dump_json(indent=2))
