"""Capability token commands: issue, list, verify, revoke, export."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console
from ..pillars.security import audit_event

from rich.table import Table


def register_token_commands(main: click.Group) -> None:
    """Register the token command group."""

    @main.group()
    def token():
        """Manage capability tokens.

        Issue, verify, list, and revoke PGP-signed capability
        tokens for fine-grained agent authorization.
        """

    @token.command("issue")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--subject", required=True, help="Who the token is for.")
    @click.option("--cap", multiple=True, required=True, help="Capabilities to grant.")
    @click.option("--ttl", default=24, help="Hours until expiry (0 = no expiry).")
    @click.option("--type", "token_type", default="capability", help="Token type.")
    @click.option("--no-sign", is_flag=True, help="Skip PGP signing.")
    def token_issue(home, subject, cap, ttl, token_type, no_sign):
        """Issue a new capability token."""
        from ..tokens import TokenType, issue_token

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        try:
            tt = TokenType(token_type)
        except ValueError:
            console.print(f"[red]Invalid token type:[/] {token_type}")
            sys.exit(1)

        ttl_hours = ttl if ttl > 0 else None
        capabilities = list(cap)

        console.print(f"\n  Issuing [cyan]{tt.value}[/] token for [bold]{subject}[/]...")
        signed = issue_token(
            home=home_path, subject=subject, capabilities=capabilities,
            token_type=tt, ttl_hours=ttl_hours, sign=not no_sign,
        )

        console.print(f"  [green]Token issued:[/] {signed.payload.token_id[:16]}...")
        console.print(f"  Capabilities: {', '.join(capabilities)}")
        if signed.payload.expires_at:
            console.print(f"  Expires: {signed.payload.expires_at.isoformat()}")
        else:
            console.print("  Expires: [yellow]never[/]")
        if signed.signature:
            console.print("  [green]PGP signed[/]")
        else:
            console.print("  [yellow]Unsigned[/]")

        audit_event(home_path, "TOKEN_ISSUE", f"Token {signed.payload.token_id[:16]} for {subject}")
        console.print()

    @token.command("list")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def token_list(home):
        """List all issued tokens."""
        from ..tokens import is_revoked, list_tokens

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/]")
            sys.exit(1)

        tokens = list_tokens(home_path)
        if not tokens:
            console.print("\n  [dim]No tokens issued yet.[/]\n")
            return

        table = Table(title="Capability Tokens", show_lines=True)
        table.add_column("ID", style="cyan", max_width=16)
        table.add_column("Type", style="bold")
        table.add_column("Subject")
        table.add_column("Capabilities")
        table.add_column("Status")
        table.add_column("Expires")

        for t in tokens:
            p = t.payload
            revoked = is_revoked(home_path, p.token_id)
            if revoked:
                st = "[red]REVOKED[/]"
            elif p.is_expired:
                st = "[yellow]EXPIRED[/]"
            elif t.signature:
                st = "[green]SIGNED[/]"
            else:
                st = "[dim]UNSIGNED[/]"

            exp_str = p.expires_at.strftime("%m/%d %H:%M") if p.expires_at else "never"
            table.add_row(p.token_id[:16], p.token_type.value, p.subject,
                          ", ".join(p.capabilities), st, exp_str)

        console.print()
        console.print(table)
        console.print()

    @token.command("verify")
    @click.argument("token_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def token_verify(token_id, home):
        """Verify a token's signature and validity."""
        from ..tokens import is_revoked, list_tokens, verify_token

        home_path = Path(home).expanduser()
        tokens = list_tokens(home_path)

        target = None
        for t in tokens:
            if t.payload.token_id.startswith(token_id):
                target = t
                break

        if not target:
            console.print(f"[red]Token not found:[/] {token_id}")
            sys.exit(1)

        if is_revoked(home_path, target.payload.token_id):
            console.print(f"\n  [red]REVOKED[/] Token {token_id[:16]} has been revoked.\n")
            sys.exit(1)

        valid = verify_token(target, home_path)
        if valid:
            console.print(f"\n  [green]VALID[/] Token {token_id[:16]}")
            console.print(f"  Subject: {target.payload.subject}")
            console.print(f"  Capabilities: {', '.join(target.payload.capabilities)}")
        else:
            console.print(f"\n  [red]INVALID[/] Token {token_id[:16]}")
            if target.payload.is_expired:
                console.print("  Reason: expired")
            else:
                console.print("  Reason: signature verification failed")
        console.print()

    @token.command("revoke")
    @click.argument("token_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def token_revoke(token_id, home):
        """Revoke a previously issued token."""
        from ..tokens import list_tokens, revoke_token

        home_path = Path(home).expanduser()
        tokens = list_tokens(home_path)

        full_id = None
        for t in tokens:
            if t.payload.token_id.startswith(token_id):
                full_id = t.payload.token_id
                break

        if not full_id:
            console.print(f"[red]Token not found:[/] {token_id}")
            sys.exit(1)

        revoke_token(home_path, full_id)
        console.print(f"\n  [red]REVOKED[/] Token {token_id[:16]}...")
        audit_event(home_path, "TOKEN_REVOKE", f"Token {token_id[:16]} revoked")
        console.print()

    @token.command("export")
    @click.argument("token_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def token_export(token_id, home):
        """Export a token as portable JSON."""
        from ..tokens import export_token, list_tokens

        home_path = Path(home).expanduser()
        tokens = list_tokens(home_path)

        target = None
        for t in tokens:
            if t.payload.token_id.startswith(token_id):
                target = t
                break

        if not target:
            console.print(f"[red]Token not found:[/] {token_id}")
            sys.exit(1)

        console.print(export_token(target))
