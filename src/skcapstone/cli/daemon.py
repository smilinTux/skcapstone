"""Daemon commands: start, stop, status, install, uninstall, logs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel


def register_daemon_commands(main: click.Group) -> None:
    """Register the daemon command group."""

    @main.group()
    def daemon():
        """Background daemon — the agent's heartbeat.

        Start the always-on daemon for inbox polling, vault sync,
        transport health monitoring, and the local status API.
        """

    @daemon.command("start")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--port", default=7777, help="API port (default: 7777).")
    @click.option("--poll", default=10, help="Inbox poll interval in seconds.")
    @click.option("--sync-interval", "sync_int", default=300, help="Vault sync interval in seconds.")
    @click.option("--foreground", is_flag=True, help="Run in foreground (don't daemonize).")
    def daemon_start(home: str, port: int, poll: int, sync_int: int, foreground: bool):
        """Start the sovereign agent daemon.

        Runs continuously, polling for messages, syncing vault state,
        and exposing a local HTTP API at http://127.0.0.1:<port>.

        Use --foreground for debugging or systemd integration.
        """
        from ..daemon import DaemonConfig, DaemonService, is_running

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        if is_running(home_path):
            console.print("[yellow]Daemon is already running.[/]")
            sys.exit(0)

        config = DaemonConfig(
            home=home_path,
            poll_interval=poll,
            sync_interval=sync_int,
            port=port,
        )
        svc = DaemonService(config)

        console.print(f"\n  [green]Starting daemon[/] on port [cyan]{port}[/]")
        console.print(f"  Poll: {poll}s | Sync: {sync_int}s")
        console.print(f"  Log: {config.log_file}")
        console.print(f"  PID: {os.getpid()}")

        if foreground:
            console.print("  [dim]Running in foreground (Ctrl+C to stop)[/]\n")
            svc.start()
            svc.run_forever()
        else:
            console.print("  [dim]Running in foreground mode (use systemd for background)[/]\n")
            svc.start()
            svc.run_forever()

    @daemon.command("stop")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def daemon_stop(home: str):
        """Stop the running daemon."""
        from ..daemon import read_pid

        home_path = Path(home).expanduser()
        pid = read_pid(home_path)

        if pid is None:
            console.print("[yellow]Daemon is not running.[/]")
            return

        import signal as sig

        try:
            os.kill(pid, sig.SIGTERM)
            console.print(f"\n  [green]Sent SIGTERM to daemon (PID {pid})[/]\n")
        except ProcessLookupError:
            console.print("[yellow]Daemon process not found — cleaning up PID file.[/]")
            (home_path / "daemon.pid").unlink(missing_ok=True)

    @daemon.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--port", default=7777, help="API port to query.")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def daemon_status(home: str, port: int, json_out: bool):
        """Show daemon status."""
        from ..daemon import get_daemon_status, is_running, read_pid

        home_path = Path(home).expanduser()
        pid = read_pid(home_path)

        if not is_running(home_path):
            if json_out:
                click.echo(json.dumps({"running": False}))
            else:
                console.print("\n  [yellow]Daemon is not running.[/]\n")
            return

        status = get_daemon_status(home_path, port)
        if json_out:
            click.echo(json.dumps(status or {"running": True, "pid": pid, "api": "unreachable"}, indent=2))
            return

        if status:
            uptime = status.get("uptime_seconds", 0)
            h, remainder = divmod(int(uptime), 3600)
            m, s = divmod(remainder, 60)
            uptime_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

            console.print()
            console.print(
                Panel(
                    f"PID: [bold]{status.get('pid')}[/]\n"
                    f"Uptime: [bold]{uptime_str}[/]\n"
                    f"Messages received: [bold]{status.get('messages_received', 0)}[/]\n"
                    f"Syncs completed: [bold]{status.get('syncs_completed', 0)}[/]\n"
                    f"Last poll: {status.get('last_poll') or '[dim]never[/]'}\n"
                    f"Last sync: {status.get('last_sync') or '[dim]never[/]'}\n"
                    f"API: [green]http://127.0.0.1:{port}[/]",
                    title="[green]Daemon Running[/]",
                    border_style="green",
                )
            )

            health = status.get("transport_health", {})
            if health:
                console.print("[bold]Transports:[/]")
                for name, info in health.items():
                    if isinstance(info, dict):
                        st = info.get("status", "unknown")
                        color = {"available": "green", "degraded": "yellow"}.get(st, "red")
                        console.print(f"  [{color}]{name}: {st.upper()}[/]")

            errors = status.get("recent_errors", [])
            if errors:
                console.print(f"\n[yellow]Recent errors ({len(errors)}):[/]")
                for err in errors[-5:]:
                    console.print(f"  [dim]{err}[/]")

            console.print()
        else:
            console.print(f"\n  [green]Daemon running[/] (PID {pid})")
            console.print(f"  [yellow]API unreachable on port {port}[/]\n")

    @daemon.command("install")
    def daemon_install():
        """Install the daemon as a systemd user service.

        Copies unit files to ~/.config/systemd/user/, enables at login,
        and starts immediately. No root required.

        Examples:

            skcapstone daemon install
        """
        from ..systemd import install_service, systemd_available

        if not systemd_available():
            console.print("[red]systemd user session not available.[/]")
            console.print("[dim]This command requires a Linux system with systemd.[/]")
            raise SystemExit(1)

        console.print("\n[cyan]Installing skcapstone systemd service...[/]")
        result = install_service()

        if result["installed"]:
            console.print("[green]  Unit files installed.[/]")
        if result["enabled"]:
            console.print("[green]  Service enabled at login.[/]")
        if result["started"]:
            console.print("[green]  Service started.[/]")
        console.print()

        if not result["installed"]:
            console.print("[red]Installation failed. Check logs.[/]")
            raise SystemExit(1)

    @daemon.command("uninstall")
    def daemon_uninstall():
        """Uninstall the systemd user service.

        Stops, disables, and removes the unit files.

        Examples:

            skcapstone daemon uninstall
        """
        from ..systemd import uninstall_service

        console.print("\n[cyan]Uninstalling skcapstone systemd service...[/]")
        result = uninstall_service()

        if result["stopped"]:
            console.print("[green]  Service stopped.[/]")
        if result["disabled"]:
            console.print("[green]  Service disabled.[/]")
        if result["removed"]:
            console.print("[green]  Unit files removed.[/]")
        console.print()

    @daemon.command("logs")
    @click.option("--lines", "-n", default=50, help="Number of lines (default: 50).")
    @click.option("--follow", "-f", is_flag=True, help="Show the command to follow logs live.")
    def daemon_logs(lines: int, follow: bool):
        """Show daemon logs from journald.

        Examples:

            skcapstone daemon logs

            skcapstone daemon logs -n 100

            skcapstone daemon logs -f
        """
        from ..systemd import service_logs

        if follow:
            cmd = service_logs(follow=True)
            console.print(f"\n  Run: [bold cyan]{cmd}[/]\n")
        else:
            output = service_logs(lines=lines)
            if output.strip():
                click.echo(output)
            else:
                console.print("[dim]No logs found. Is the service installed?[/]")
