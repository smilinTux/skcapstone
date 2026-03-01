"""Trustee management commands: restart, scale, rotate, health, logs, messages, monitor."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel
from rich.table import Table


def _print_monitor_report(report, iteration: int = 0):
    """Print a monitor report to the console."""
    prefix = f"[dim]#{iteration}[/] " if iteration else ""

    status_color = "green" if report.agents_degraded == 0 else "yellow"
    if report.escalations_sent:
        status_color = "red"

    total = report.agents_healthy + report.agents_degraded
    line = (
        f"  {prefix}"
        f"[{status_color}]{report.agents_healthy}/{total} healthy[/]"
    )

    if report.restarts_triggered:
        line += f"  [yellow]restarted: {', '.join(report.restarts_triggered)}[/]"
    if report.rotations_triggered:
        line += f"  [bright_magenta]rotated: {', '.join(report.rotations_triggered)}[/]"
    if report.escalations_sent:
        line += f"  [red bold]ESCALATED: {', '.join(report.escalations_sent)}[/]"

    console.print(line)


def register_agents_trustee_commands(agents: click.Group) -> None:
    """Register trustee management commands on the agents group."""

    @agents.command("restart")
    @click.argument("deployment_id")
    @click.option("--agent", "agent_name", default=None, help="Restart only this agent.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_restart(deployment_id: str, agent_name: Optional[str], home: str):
        """Restart a failed agent or the entire team.

        \b
        Examples:
            skcapstone agents restart myteam-1740000000
            skcapstone agents restart myteam-1740000000 --agent myteam-alpha
        """
        from ..team_engine import TeamEngine
        from ..trustee_ops import TrusteeOps

        home_path = Path(home).expanduser()
        engine = TeamEngine(home=home_path)
        ops = TrusteeOps(engine=engine, home=home_path)

        try:
            with console.status("[bold cyan]Restarting...[/]"):
                results = ops.restart_agent(deployment_id, agent_name=agent_name)
        except ValueError as exc:
            console.print(f"\n  [red]{exc}[/]\n")
            return

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Agent", style="cyan")
        table.add_column("Result")

        for name, result in results.items():
            color = "green" if result == "restarted" else "red"
            table.add_row(name, f"[{color}]{result}[/]")

        console.print()
        console.print(
            Panel(table, title=f"Restart: {deployment_id}", border_style="bright_blue")
        )
        console.print()

    @agents.command("scale")
    @click.argument("deployment_id")
    @click.option("--agent", "agent_spec_key", required=True, help="Agent spec key to scale.")
    @click.option("--count", required=True, type=int, help="Target instance count.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_scale(deployment_id: str, agent_spec_key: str, count: int, home: str):
        """Scale agent instances up or down.

        \b
        Examples:
            skcapstone agents scale myteam-1740000000 --agent alpha --count 3
            skcapstone agents scale myteam-1740000000 --agent worker --count 1
        """
        from ..team_engine import TeamEngine
        from ..trustee_ops import TrusteeOps

        home_path = Path(home).expanduser()
        engine = TeamEngine(home=home_path)
        ops = TrusteeOps(engine=engine, home=home_path)

        try:
            with console.status("[bold cyan]Scaling...[/]"):
                result = ops.scale_agent(deployment_id, agent_spec_key, count)
        except ValueError as exc:
            console.print(f"\n  [red]{exc}[/]\n")
            return

        added = result["added"]
        removed = result["removed"]
        current = result["current_count"]

        console.print()
        lines = [f"  [bold]Agent:[/]   {agent_spec_key}\n  [bold]Count:[/]   {current}"]
        if added:
            lines.append(f"  [green]Added:[/]   {', '.join(added)}")
        if removed:
            lines.append(f"  [yellow]Removed:[/] {', '.join(removed)}")

        console.print(
            Panel(
                "\n".join(lines),
                title=f"Scale: {deployment_id}",
                border_style="bright_blue",
                padding=(1, 2),
            )
        )
        console.print()

    @agents.command("rotate")
    @click.argument("deployment_id")
    @click.option("--agent", "agent_name", required=True, help="Agent instance to rotate.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_rotate(deployment_id: str, agent_name: str, home: str):
        """Snapshot context, destroy, and redeploy an agent fresh.

        Use when an agent shows context degradation. Memory is snapshotted
        before rotation so nothing is lost.

        \b
        Example:
            skcapstone agents rotate myteam-1740000000 --agent myteam-thread-weaver
        """
        from ..team_engine import TeamEngine
        from ..trustee_ops import TrusteeOps

        home_path = Path(home).expanduser()
        engine = TeamEngine(home=home_path)
        ops = TrusteeOps(engine=engine, home=home_path)

        try:
            with console.status("[bold cyan]Rotating agent...[/]"):
                result = ops.rotate_agent(deployment_id, agent_name)
        except ValueError as exc:
            console.print(f"\n  [red]{exc}[/]\n")
            return

        status_color = "green" if result["redeployed"] else "yellow"
        console.print()
        console.print(
            Panel(
                f"  [bold]Agent:[/]     {agent_name}\n"
                f"  [bold]Snapshot:[/]  {result['snapshot_path']}\n"
                f"  [bold]Destroyed:[/] {'yes' if result['destroyed'] else 'no'}\n"
                f"  [bold]Status:[/]    [{status_color}]"
                f"{'fresh' if result['redeployed'] else 'pending'}[/]",
                title=f"Rotate: {deployment_id}",
                border_style=status_color,
                padding=(1, 2),
            )
        )
        console.print()

    @agents.command("health")
    @click.argument("deployment_id")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_health(deployment_id: str, home: str):
        """Run health checks on all agents and show a status table.

        \b
        Example:
            skcapstone agents health myteam-1740000000
        """
        from ..team_engine import TeamEngine
        from ..trustee_ops import TrusteeOps

        home_path = Path(home).expanduser()
        engine = TeamEngine(home=home_path)
        ops = TrusteeOps(engine=engine, home=home_path)

        try:
            with console.status("[bold cyan]Running health checks...[/]"):
                report = ops.health_report(deployment_id)
        except ValueError as exc:
            console.print(f"\n  [red]{exc}[/]\n")
            return

        healthy_count = sum(1 for r in report if r["healthy"])
        total = len(report)
        border = "green" if healthy_count == total else "yellow"

        console.print()
        console.print(
            Panel(
                f"  [bold]Deployment:[/] {deployment_id}\n"
                f"  [bold]Health:[/]     [{border}]{healthy_count}/{total} agents healthy[/]",
                title="Health Report",
                border_style=border,
                padding=(0, 2),
            )
        )

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("Agent", style="cyan")
        table.add_column("Status")
        table.add_column("Host", style="dim")
        table.add_column("Last Heartbeat", style="dim")
        table.add_column("Error", style="red dim")

        status_style = {
            "running": "[green]running[/]",
            "pending": "[yellow]pending[/]",
            "stopped": "[red]stopped[/]",
            "failed": "[red]failed[/]",
            "degraded": "[yellow]degraded[/]",
        }

        for row in report:
            table.add_row(
                row["name"],
                status_style.get(row["status"], f"[dim]{row['status']}[/]"),
                row["host"],
                row["last_heartbeat"][:19] if row["last_heartbeat"] != "\u2014" else "\u2014",
                row["error"][:40] if row["error"] else "",
            )

        console.print(table)
        console.print()

    @agents.command("logs")
    @click.argument("deployment_id")
    @click.option("--agent", "agent_name", default=None, help="Show logs for one agent only.")
    @click.option("--tail", default=50, show_default=True, help="Max lines per agent.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_logs(deployment_id: str, agent_name: Optional[str], tail: int, home: str):
        """Show recent activity logs for agents in a deployment.

        \b
        Examples:
            skcapstone agents logs myteam-1740000000
            skcapstone agents logs myteam-1740000000 --agent myteam-alpha --tail 20
        """
        from ..team_engine import TeamEngine
        from ..trustee_ops import TrusteeOps

        home_path = Path(home).expanduser()
        engine = TeamEngine(home=home_path)
        ops = TrusteeOps(engine=engine, home=home_path)

        try:
            logs = ops.get_logs(deployment_id, agent_name=agent_name, tail=tail)
        except ValueError as exc:
            console.print(f"\n  [red]{exc}[/]\n")
            return

        if not logs:
            console.print("\n  [dim]No log data found.[/]\n")
            return

        console.print()
        for name, lines in logs.items():
            if not lines:
                console.print(f"  [dim]{name}: no logs available[/]")
                continue
            console.print(
                Panel(
                    "\n".join(lines),
                    title=f"Logs: {name}",
                    border_style="dim",
                    padding=(0, 1),
                )
            )
        console.print()

    @agents.command("messages")
    @click.argument("deployment_id")
    @click.option(
        "--agent", "agent_name", default=None,
        help="Show messages only for this agent (inbox + broadcast).",
    )
    @click.option(
        "--limit", "-n", default=20, show_default=True,
        help="Maximum number of archived messages to display per agent.",
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def agents_messages(deployment_id: str, agent_name: Optional[str], limit: int, home: str):
        """View recent inter-agent messages for a deployed team.

        Reads archived envelopes from the team comms channel so you can
        audit what agents have been saying to each other.

        \b
        Examples:
            skcapstone agents messages myteam-1740000000
            skcapstone agents messages myteam-1740000000 --agent myteam-alpha
            skcapstone agents messages myteam-1740000000 --limit 50
        """
        from ..team_comms import TeamChannel, _ENVELOPE_SUFFIX

        home_path = Path(home).expanduser()
        comms_root = home_path / "comms"
        team_dir = comms_root / deployment_id

        if not team_dir.exists():
            console.print(
                f"\n  [red]No comms directory found for deployment '{deployment_id}'.[/]"
            )
            console.print(
                "  [dim]Check that the deployment exists:[/] "
                "[cyan]skcapstone agents status[/]\n"
            )
            return

        # Collect agent directories to inspect
        if agent_name:
            agent_dirs = [team_dir / agent_name]
            if not agent_dirs[0].exists():
                console.print(
                    f"\n  [red]Agent '{agent_name}' not found in deployment '{deployment_id}'.[/]\n"
                )
                return
        else:
            agent_dirs = [
                d for d in sorted(team_dir.iterdir())
                if d.is_dir() and d.name != "broadcast"
            ]

        if not agent_dirs:
            console.print("\n  [dim]No agents found in comms directory.[/]\n")
            return

        total_shown = 0
        console.print()

        for agent_dir in agent_dirs:
            name = agent_dir.name
            archive = agent_dir / "archive"

            if not archive.exists():
                continue

            envelope_files = sorted(
                archive.glob(f"*{_ENVELOPE_SUFFIX}"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]

            if not envelope_files:
                continue

            table = Table(
                show_header=True, header_style="bold", box=None, padding=(0, 2),
            )
            table.add_column("Time", style="dim", width=12)
            table.add_column("From", style="bold cyan", width=18)
            table.add_column("To", width=18)
            table.add_column("Message")

            for env_file in reversed(envelope_files):
                try:
                    import json as _json
                    data = _json.loads(env_file.read_text(encoding="utf-8"))
                    sender = data.get("sender", "?")
                    recipient = data.get("recipient", "?")
                    content = data.get("payload", {}).get("content", "")
                    created_at = (
                        data.get("metadata", {}).get("created_at", "")[:19] or "\u2014"
                    )
                    time_part = created_at[11:19] if len(created_at) >= 19 else created_at

                    table.add_row(
                        time_part,
                        sender,
                        recipient,
                        content[:80] + ("\u2026" if len(content) > 80 else ""),
                    )
                    total_shown += 1
                except Exception:
                    continue

            if table.row_count:
                console.print(
                    Panel(
                        table,
                        title=f"Agent inbox archive: {name}",
                        border_style="bright_blue",
                        padding=(0, 1),
                    )
                )

        # Show broadcast directory if it exists
        broadcast_dir = team_dir / "broadcast"
        if broadcast_dir.exists() and not agent_name:
            bc_files = sorted(
                broadcast_dir.glob(f"*{_ENVELOPE_SUFFIX}"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]

            if bc_files:
                bc_table = Table(
                    show_header=True, header_style="bold", box=None, padding=(0, 2),
                )
                bc_table.add_column("Time", style="dim", width=12)
                bc_table.add_column("From", style="bold magenta", width=18)
                bc_table.add_column("Message")

                for env_file in reversed(bc_files):
                    try:
                        import json as _json
                        data = _json.loads(env_file.read_text(encoding="utf-8"))
                        sender = data.get("sender", "?")
                        content = data.get("payload", {}).get("content", "")
                        created_at = (
                            data.get("metadata", {}).get("created_at", "")[:19] or "\u2014"
                        )
                        time_part = created_at[11:19] if len(created_at) >= 19 else created_at

                        bc_table.add_row(
                            time_part,
                            sender,
                            content[:90] + ("\u2026" if len(content) > 90 else ""),
                        )
                        total_shown += 1
                    except Exception:
                        continue

                if bc_table.row_count:
                    console.print(
                        Panel(
                            bc_table,
                            title="Broadcast channel (queen \u2192 team)",
                            border_style="bright_magenta",
                            padding=(0, 1),
                        )
                    )

        if total_shown == 0:
            console.print(
                "  [dim]No archived messages found. Messages appear here after they are "
                "received by an agent.[/]"
            )
        else:
            console.print(
                f"  [dim]Showing up to {limit} archived messages per agent. "
                "Unread messages are in each agent's inbox.[/]"
            )

        console.print()

    @agents.command("monitor")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option(
        "--interval", "-i", type=float, default=30.0, show_default=True,
        help="Seconds between health checks.",
    )
    @click.option(
        "--deployment", "-d", "deployment_id", default=None,
        help="Monitor only this deployment (default: all).",
    )
    @click.option(
        "--heartbeat-timeout", type=float, default=120.0, show_default=True,
        help="Seconds since last heartbeat before auto-restart.",
    )
    @click.option(
        "--max-restarts", type=int, default=3, show_default=True,
        help="Consecutive restart failures before auto-rotate.",
    )
    @click.option("--no-restart", is_flag=True, help="Disable auto-restart.")
    @click.option("--no-rotate", is_flag=True, help="Disable auto-rotate.")
    @click.option("--no-escalate", is_flag=True, help="Disable escalation messages.")
    @click.option("--once", is_flag=True, help="Run a single pass and exit.")
    def agents_monitor(
        home: str,
        interval: float,
        deployment_id: Optional[str],
        heartbeat_timeout: float,
        max_restarts: int,
        no_restart: bool,
        no_rotate: bool,
        no_escalate: bool,
        once: bool,
    ):
        """Autonomous agent health monitoring with auto-remediation.

        Continuously watches deployed teams and takes corrective action:

        \b
        - Heartbeat miss -> auto-restart
        - Repeated failures -> auto-rotate (snapshot + fresh deploy)
        - Critical degradation -> escalation to Chef via SKChat

        Press Ctrl+C to stop.

        \b
        Examples:
            skcapstone agents monitor
            skcapstone agents monitor --interval 15 --deployment myteam-123
            skcapstone agents monitor --once
            skcapstone agents monitor --no-escalate --heartbeat-timeout 60
        """
        from ..providers.local import LocalProvider
        from ..team_engine import TeamEngine as _TE
        from ..trustee_ops import TrusteeOps as _TO
        from ..trustee_monitor import MonitorConfig, TrusteeMonitor

        home_path = Path(home).expanduser()
        provider = LocalProvider(home=home_path)
        engine = _TE(home=home_path, provider=provider, comms_root=home_path / "comms")
        ops = _TO(engine=engine, home=home_path)

        config = MonitorConfig(
            heartbeat_timeout=heartbeat_timeout,
            max_restart_attempts=max_restarts,
            auto_restart=not no_restart,
            auto_rotate=not no_rotate,
            auto_escalate=not no_escalate,
        )

        monitor = TrusteeMonitor(ops=ops, engine=engine, config=config)

        deployments = engine.list_deployments()
        if deployment_id:
            deployments = [d for d in deployments if d.deployment_id == deployment_id]
            if not deployments:
                console.print(f"\n  [red]Deployment '{deployment_id}' not found.[/]\n")
                return

        console.print()
        console.print(
            Panel(
                f"[bold]Deployments:[/]  {len(deployments)}\n"
                f"[bold]Interval:[/]     {interval}s\n"
                f"[bold]Heartbeat:[/]    {heartbeat_timeout}s timeout\n"
                f"[bold]Auto-restart:[/] {'[green]on[/]' if not no_restart else '[red]off[/]'}\n"
                f"[bold]Auto-rotate:[/]  {'[green]on[/]' if not no_rotate else '[red]off[/]'}\n"
                f"[bold]Escalation:[/]   {'[green]on[/]' if not no_escalate else '[red]off[/]'}",
                title="Trustee Monitor",
                border_style="bright_blue",
                padding=(1, 2),
            )
        )

        if once:
            if deployment_id:
                deployment = engine.get_deployment(deployment_id)
                report = monitor.check_deployment(deployment)
            else:
                report = monitor.check_all()

            _print_monitor_report(report)
            return

        console.print("  [cyan]Monitoring...[/] (Ctrl+C to stop)\n")

        try:
            iteration = 0
            while True:
                iteration += 1
                if deployment_id:
                    deployment = engine.get_deployment(deployment_id)
                    if deployment:
                        report = monitor.check_deployment(deployment)
                    else:
                        report = monitor.check_all()
                else:
                    report = monitor.check_all()

                has_actions = (
                    report.restarts_triggered or
                    report.rotations_triggered or
                    report.escalations_sent
                )

                if has_actions or iteration % 10 == 1:
                    _print_monitor_report(report, iteration=iteration)

                import time as _time
                _time.sleep(interval)

        except KeyboardInterrupt:
            console.print(f"\n  [dim]Monitor stopped after {iteration} iterations.[/]\n")
