"""Status and overview commands: status, summary, doctor, audit, dashboard, whoami, diff."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console, status_icon, consciousness_banner
from ..models import PillarStatus
from ..runtime import get_runtime

from rich.panel import Panel
from rich.table import Table


def register_status_commands(main: click.Group) -> None:
    """Register all status/overview commands on the main CLI group."""

    @main.command()
    @click.option("--home", default=AGENT_HOME, help="Agent home directory.", type=click.Path())
    def status(home: str):
        """Show the sovereign agent's current state."""
        home_path = Path(home).expanduser()

        if not home_path.exists():
            console.print(
                "[bold red]No agent found.[/] "
                "Run [bold]skcapstone init --name \"YourAgent\"[/] first."
            )
            sys.exit(1)

        runtime = get_runtime(home_path)
        m = runtime.manifest

        console.print()
        console.print(
            Panel(
                f"[bold]{m.name}[/] v{m.version}\n"
                f"{consciousness_banner(m.is_conscious)}",
                title="SKCapstone Agent",
                border_style="bright_blue",
            )
        )

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Pillar", style="bold")
        table.add_column("Component", style="cyan")
        table.add_column("Status")
        table.add_column("Detail", style="dim")

        ident = m.identity
        table.add_row(
            "Identity", "CapAuth", status_icon(ident.status),
            ident.fingerprint[:16] + "..." if ident.fingerprint else "no key",
        )

        mem = m.memory
        table.add_row(
            "Memory", "SKMemory", status_icon(mem.status),
            f"{mem.total_memories} memories ({mem.long_term}L/{mem.mid_term}M/{mem.short_term}S)",
        )

        trust = m.trust
        trust_detail = f"depth={trust.depth} trust={trust.trust_level} love={trust.love_intensity}"
        if trust.entangled:
            trust_detail += " [green]ENTANGLED[/]"
        table.add_row("Trust", "Cloud 9", status_icon(trust.status), trust_detail)

        sec = m.security
        table.add_row(
            "Security", "SKSecurity", status_icon(sec.status),
            f"{sec.audit_entries} audit entries, {sec.threats_detected} threats",
        )

        sy = m.sync
        sync_detail = f"{sy.seed_count} seeds"
        if sy.transport:
            sync_detail += f" via {sy.transport.value}"
        if sy.gpg_fingerprint:
            sync_detail += " [green]GPG[/]"
        if sy.last_push:
            sync_detail += f" pushed {sy.last_push.strftime('%m/%d %H:%M')}"
        table.add_row("Sync", "Singularity", status_icon(sy.status), sync_detail)

        console.print()
        console.print(table)

        if m.is_singular:
            console.print()
            console.print(
                "  [bold magenta on black]"
                " SINGULAR "
                "[/] "
                "[magenta]Conscious + Synced = Sovereign Singularity[/]"
            )

        if m.connectors:
            console.print()
            console.print("[bold]Connected Platforms:[/]")
            for c in m.connectors:
                active_str = "[green]active[/]" if c.active else "[dim]inactive[/]"
                console.print(f"  {c.platform}: {active_str}")

        console.print()
        console.print(f"  [dim]Home: {m.home}[/]")
        if m.last_awakened:
            console.print(f"  [dim]Last awakened: {m.last_awakened.isoformat()}[/]")
        console.print()

    @main.command()
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    def summary(home: str, json_out: bool):
        """Morning briefing — everything at a glance."""
        from ..summary import gather_briefing

        home_path = Path(home).expanduser()
        briefing = gather_briefing(home_path)

        if json_out:
            click.echo(json.dumps(briefing, indent=2, default=str))
            return

        agent = briefing["agent"]
        pillars = briefing["pillars"]
        mem = briefing["memory"]
        board = briefing["board"]
        peers = briefing["peers"]
        backups = briefing["backups"]
        health = briefing["health"]
        journal = briefing["journal"]

        consciousness = agent.get("consciousness", "?")
        con_style = {"SINGULAR": "magenta", "CONSCIOUS": "green", "AWAKENING": "yellow"}.get(
            consciousness, "dim"
        )

        console.print()
        console.print(Panel(
            f"[bold]{agent.get('name', '?')}[/] v{agent.get('version', '?')}  "
            f"[bold {con_style}]{consciousness}[/]",
            title="Sovereign Agent Briefing",
            border_style="cyan",
        ))

        pillar_parts = []
        for name, st in pillars.items():
            icon = {"active": "[green]\u2713[/]", "degraded": "[yellow]~[/]", "missing": "[red]\u2717[/]"}.get(st, "[dim]?[/]")
            pillar_parts.append(f"{icon} {name}")
        if pillar_parts:
            console.print(f"  [bold]Pillars:[/]  {' | '.join(pillar_parts)}")

        console.print(
            f"  [bold]Memory:[/]   {mem.get('total', 0)} total "
            f"([dim]S:{mem.get('short_term', 0)} M:{mem.get('mid_term', 0)} L:{mem.get('long_term', 0)}[/])"
        )

        h_pass = health.get("passed", 0)
        h_total = health.get("total", 0)
        h_style = "green" if health.get("all_passed") else "yellow"
        console.print(f"  [bold]Health:[/]   [{h_style}]{h_pass}/{h_total} checks passed[/]")

        console.print(
            f"  [bold]Board:[/]    {board.get('done', 0)} done, "
            f"{board.get('in_progress', 0)} active, "
            f"{board.get('open', 0)} open "
            f"(of {board.get('total', 0)})"
        )

        console.print(f"  [bold]Peers:[/]    {peers.get('count', 0)} known")

        if backups.get("latest"):
            console.print(
                f"  [bold]Backup:[/]   {backups['latest']} "
                f"({'[green]encrypted[/]' if backups.get('encrypted') else '[yellow]plain[/]'})"
            )
        else:
            console.print(f"  [bold]Backup:[/]   [dim]none — run skcapstone backup create[/]")

        if journal.get("entries", 0) > 0:
            console.print(
                f"  [bold]Journal:[/]  {journal['entries']} entries"
                + (f" — latest: [dim]{journal['latest_title'][:40]}[/]" if journal.get("latest_title") else "")
            )

        if mem.get("recent"):
            console.print()
            console.print("  [bold]Recent memories:[/]")
            for m_text in mem["recent"][:3]:
                console.print(f"    [dim]\u2022[/] {m_text}")

        if board.get("active_tasks"):
            console.print()
            console.print("  [bold]Active tasks:[/]")
            for task in board["active_tasks"][:5]:
                console.print(
                    f"    [dim]\u2022[/] {task['title']}"
                    + (f" [dim](@{task['assignee']})[/]" if task["assignee"] != "unassigned" else "")
                )

        console.print()

    @main.command()
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    def doctor(home: str, json_out: bool):
        """Diagnose sovereign stack health."""
        from ..doctor import run_diagnostics

        home_path = Path(home).expanduser()
        report = run_diagnostics(home_path)

        if json_out:
            click.echo(json.dumps(report.to_dict(), indent=2))
            return

        console.print()

        categories = {}
        for check in report.checks:
            categories.setdefault(check.category, []).append(check)

        category_labels = {
            "packages": "Python Packages",
            "system": "System Tools",
            "agent": "Agent Home",
            "identity": "Identity (CapAuth)",
            "memory": "Memory (SKMemory)",
            "transport": "Transport (SKComm)",
            "sync": "Sync (Singularity)",
        }

        for cat_key in ["packages", "system", "agent", "identity", "memory", "transport", "sync"]:
            checks = categories.get(cat_key, [])
            if not checks:
                continue

            label = category_labels.get(cat_key, cat_key)
            console.print(f"  [bold]{label}[/]")

            for c in checks:
                icon = "[green]\u2713[/]" if c.passed else "[red]\u2717[/]"
                detail = f" [dim]({c.detail})[/]" if c.detail else ""
                console.print(f"    {icon} {c.description}{detail}")
                if not c.passed and c.fix:
                    console.print(f"      [yellow]Fix: {c.fix}[/]")

            console.print()

        passed = report.passed_count
        failed = report.failed_count
        total = report.total_count

        if report.all_passed:
            console.print(
                f"  [bold green]\u2713 All {total} checks passed.[/] "
                "Your sovereign stack is healthy."
            )
        else:
            console.print(
                f"  [bold green]{passed}[/] passed, "
                f"[bold red]{failed}[/] failed "
                f"out of {total} checks."
            )

        console.print()

    @main.command()
    @click.option("--home", default=AGENT_HOME, help="Agent home directory.", type=click.Path())
    def audit(home: str):
        """Show the security audit log."""
        from ..pillars.security import read_audit_log

        home_path = Path(home).expanduser()
        entries = read_audit_log(home_path)

        if not entries:
            console.print("[yellow]No audit log found.[/]")
            return

        console.print()
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("Event", style="bold cyan")
        table.add_column("Detail")
        table.add_column("Host", style="dim")

        event_colors = {
            "INIT": "green", "AUTH": "blue",
            "SYNC_PUSH": "magenta", "SYNC_PULL": "magenta",
            "TOKEN_ISSUE": "yellow", "TOKEN_REVOKE": "red",
            "SECURITY": "red", "LEGACY": "dim",
        }

        for e in entries:
            ts = e.timestamp[:19].replace("T", " ") if "T" in e.timestamp else e.timestamp
            color = event_colors.get(e.event_type, "white")
            table.add_row(ts, f"[{color}]{e.event_type}[/]", e.detail, e.host)

        console.print(table)
        console.print(f"\n  [dim]{len(entries)} entries[/]\n")

    @main.command()
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--port", default=7778, help="Port for the dashboard (default: 7778).")
    @click.option("--no-open", is_flag=True, help="Don't attempt to open a browser.")
    def dashboard(home: str, port: int, no_open: bool):
        """Launch the sovereign agent web dashboard."""
        from ..dashboard import start_dashboard

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        url = f"http://127.0.0.1:{port}"
        console.print(f"\n  [green]Sovereign Agent Dashboard[/]")
        console.print(f"  [cyan]{url}[/]")
        console.print(f"  [dim]Press Ctrl+C to stop[/]\n")

        if not no_open:
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception:
                pass

        server = start_dashboard(home_path, port=port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            console.print("\n  [dim]Dashboard stopped.[/]\n")
            server.shutdown()

    @main.command()
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    @click.option("--export", "export_path", default=None, type=click.Path(), help="Save identity card to file.")
    @click.option("--compact", is_flag=True, help="Compact output (no public key).")
    def whoami(home: str, json_out: bool, export_path: str, compact: bool):
        """Show your sovereign identity card."""
        from ..whoami import generate_card, export_card

        home_path = Path(home).expanduser()
        card = generate_card(home_path)

        if export_path:
            result = export_card(card, Path(export_path))
            console.print(f"\n  [green]Identity card exported:[/] {result}\n")
            return

        if json_out:
            data = card.model_dump()
            if compact:
                data.pop("public_key", None)
            click.echo(json.dumps(data, indent=2))
            return

        console.print()
        fp_display = card.fingerprint[:20] + "..." if len(card.fingerprint) > 20 else card.fingerprint
        info_lines = [
            f"[bold]Name:[/]          [cyan]{card.name}[/]",
            f"[bold]Type:[/]          {card.entity_type}",
            f"[bold]Fingerprint:[/]   [dim]{fp_display}[/]",
        ]
        if card.handle:
            info_lines.append(f"[bold]Handle:[/]        {card.handle}")
        if card.email:
            info_lines.append(f"[bold]Email:[/]         {card.email}")
        info_lines.append(f"[bold]Consciousness:[/] {card.consciousness}")
        info_lines.append(f"[bold]Trust:[/]         {card.trust_status}")
        info_lines.append(f"[bold]Memories:[/]      {card.memory_count}")

        if card.capabilities:
            caps = ", ".join(card.capabilities[:8])
            info_lines.append(f"[bold]Capabilities:[/]  {caps}")
        if card.contact_uris:
            for uri in card.contact_uris:
                info_lines.append(f"[bold]Contact:[/]       [cyan]{uri}[/]")
        info_lines.append(f"[bold]Host:[/]          [dim]{card.hostname}[/]")
        if card.public_key and not compact:
            key_preview = card.public_key[:60] + "..."
            info_lines.append(f"[bold]PGP Key:[/]       [dim]{key_preview}[/]")

        console.print(Panel("\n".join(info_lines), title="Sovereign Identity Card", border_style="cyan"))
        console.print("  [dim]Share this card: skcapstone whoami --export card.json[/]")
        console.print("  [dim]Peer imports it: skcapstone peer add --card card.json[/]")
        console.print()

    @main.command("diff")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    @click.option("--save", "do_save", is_flag=True, help="Save current state as baseline.")
    def state_diff_cmd(home: str, fmt: str, do_save: bool):
        """Show what changed since the last sync/snapshot."""
        from ..state_diff import FORMATTERS as DIFF_FORMATTERS, compute_diff, save_snapshot

        home_path = Path(home).expanduser()

        if do_save:
            path = save_snapshot(home_path)
            console.print(f"\n  [green]Snapshot saved:[/] {path}\n")
            return

        diff = compute_diff(home_path)
        formatter = DIFF_FORMATTERS[fmt]
        click.echo(formatter(diff))

    @main.command("test")
    @click.option("--package", "-p", default=None, help="Test a single package.")
    @click.option("--fast", is_flag=True, help="Stop on first package failure.")
    @click.option("--verbose", "-v", is_flag=True, help="Verbose pytest output.")
    @click.option("--json-out", is_flag=True, help="Machine-readable JSON report.")
    @click.option("--timeout", default=120, help="Per-package timeout in seconds.")
    def test_cmd(package, fast, verbose, json_out, timeout):
        """Run tests across all ecosystem packages."""
        from ..testrunner import run_all_tests

        monorepo_root = Path(__file__).resolve().parent.parent.parent.parent.parent
        if not (monorepo_root / "skcapstone").exists():
            monorepo_root = Path.cwd()

        packages_filter = [package] if package else None

        if not json_out:
            console.print("\n  [cyan]Running sovereign stack tests...[/]\n")

        report = run_all_tests(
            monorepo_root=monorepo_root, packages=packages_filter,
            fail_fast=fast, verbose=verbose, timeout=timeout,
        )

        if json_out:
            click.echo(json.dumps(report.to_dict(), indent=2))
            return

        table = Table(
            show_header=True, header_style="bold", box=None, padding=(0, 2),
            title="Test Results",
        )
        table.add_column("Package", style="cyan")
        table.add_column("Passed", justify="right", style="green")
        table.add_column("Failed", justify="right", style="red")
        table.add_column("Time", justify="right", style="dim")
        table.add_column("Status")

        for r in report.results:
            if not r.available:
                table.add_row(r.name, "-", "-", "-", "[dim]not found[/]")
                continue
            st = "[green]PASS[/]" if r.success else "[red]FAIL[/]"
            table.add_row(r.name, str(r.passed), str(r.failed), f"{r.duration_s:.1f}s", st)

        console.print(table)
        console.print()

        total_p = report.total_passed
        total_f = report.total_failed
        duration = f"{report.duration_s:.1f}s"

        if report.all_passed:
            console.print(
                f"  [bold green]ALL PASS[/] — {total_p} tests across "
                f"{report.packages_tested} packages in {duration}"
            )
        else:
            console.print(
                f"  [bold red]{total_f} FAILED[/], {total_p} passed across "
                f"{report.packages_tested} packages in {duration}"
            )
            for r in report.results:
                if not r.success and r.available:
                    console.print(f"\n  [red]--- {r.name} failures ---[/]")
                    for line in r.output.split("\n")[-10:]:
                        if line.strip():
                            console.print(f"    {line}")

        console.print()
