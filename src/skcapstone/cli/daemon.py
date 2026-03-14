"""Daemon commands: start, stop, status, install, uninstall, logs."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.console import Console
from rich.panel import Panel

from .. import AGENT_PORTS, DEFAULT_PORT, SKCAPSTONE_ROOT


def _resolve_agent_home(agent: str | None, home: str) -> Path:
    """Return the effective agent home directory.

    If *agent* is given the home is always
    ``~/.skcapstone/agents/<agent>/`` regardless of *home*.
    Otherwise *home* is used verbatim (backward-compat default).
    """
    if agent:
        return (Path(SKCAPSTONE_ROOT) / "agents" / agent).expanduser()
    return Path(home).expanduser()


def _resolve_agent_port(agent: str | None, explicit_port: int | None) -> int:
    """Return the port for *agent*, falling back to *explicit_port* or 7777."""
    if explicit_port is not None:
        return explicit_port
    if agent:
        return AGENT_PORTS.get(agent, max(AGENT_PORTS.values(), default=DEFAULT_PORT) + 1)
    return DEFAULT_PORT


_DEV_AUTH_BANNER = """\
[bold red]WARNING: SKCAPSTONE_DEV_AUTH is enabled[/]

[red]Dev-auth mode bypasses cryptographic identity verification.[/]
[red]Any caller can claim any agent identity without a valid PGP token.[/]

[yellow]This is only safe on a fully isolated development machine.[/]
[yellow]NEVER run with SKCAPSTONE_DEV_AUTH=true in production.[/]"""


def _warn_dev_auth(auto_confirm: bool) -> None:
    """Print a prominent warning when dev-auth mode is active.

    If *auto_confirm* is False and neither CI nor SKCAPSTONE_YES env vars are
    set, block until the operator presses Enter to confirm they understand the
    risk.  This gives the operator a chance to abort (Ctrl+C) before the daemon
    starts accepting unauthenticated requests.
    """
    raw = os.environ.get("SKCAPSTONE_DEV_AUTH", "").strip().lower()
    if raw not in ("1", "true", "yes"):
        return

    stderr_console = Console(stderr=True, highlight=False)
    stderr_console.print()
    stderr_console.print(
        Panel(
            _DEV_AUTH_BANNER,
            title="[bold red on white] !! DEV AUTH MODE !! [/]",
            border_style="bold red",
            padding=(1, 4),
        )
    )
    stderr_console.print()

    skip = (
        auto_confirm
        or os.environ.get("CI", "").strip().lower() in ("1", "true", "yes")
        or os.environ.get("SKCAPSTONE_YES", "").strip().lower() in ("1", "true", "yes")
    )
    if skip:
        stderr_console.print(
            "[yellow]Dev-auth warning acknowledged automatically (--yes / CI mode).[/]\n"
        )
        return

    try:
        input("  Press Enter to acknowledge and continue, or Ctrl+C to abort... ")
    except (KeyboardInterrupt, EOFError):
        stderr_console.print("\n[red]Aborted.[/]")
        sys.exit(1)

    stderr_console.print()


def register_daemon_commands(main: click.Group) -> None:
    """Register the daemon command group."""

    @main.group()
    def daemon():
        """Background daemon — the agent's heartbeat.

        Start the always-on daemon for inbox polling, vault sync,
        transport health monitoring, and the local status API.
        """

    @daemon.command("start")
    @click.option("--agent", default=None, help="Named agent to start (e.g. opus, jarvis).")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--port", default=None, type=int, help="API port (auto-assigned per agent).")
    @click.option("--poll", default=10, help="Inbox poll interval in seconds.")
    @click.option("--sync-interval", "sync_int", default=300, help="Vault sync interval in seconds.")
    @click.option("--foreground", is_flag=True, help="Run in foreground (don't daemonize).")
    @click.option("--no-consciousness", "no_consciousness", is_flag=True,
                  help="Disable the consciousness loop.")
    @click.option("--yes", "-y", "auto_confirm", is_flag=True,
                  help="Skip interactive confirmation prompts (for CI/automation).")
    def daemon_start(agent: str | None, home: str, port: int | None, poll: int, sync_int: int,
                     foreground: bool, no_consciousness: bool, auto_confirm: bool):
        """Start the sovereign agent daemon.

        Runs continuously, polling for messages, syncing vault state,
        and exposing a local HTTP API at http://127.0.0.1:<port>.

        Use --agent to run a named agent instance (opus, jarvis, …).
        Each named agent uses its own home directory, port, and PID file
        so multiple agents can run simultaneously.

        Use --foreground for debugging or systemd integration.
        Use --no-consciousness to disable autonomous message processing.

        Examples:

            skcapstone daemon start --agent opus

            skcapstone daemon start --agent jarvis --foreground
        """
        from ..daemon import DaemonConfig, DaemonService, is_running

        home_path = _resolve_agent_home(agent, home)
        effective_port = _resolve_agent_port(agent, port)

        if agent:
            # Propagate identity to child imports that read SKCAPSTONE_AGENT.
            os.environ["SKCAPSTONE_AGENT"] = agent

        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        if is_running(home_path):
            console.print("[yellow]Daemon is already running.[/]")
            sys.exit(0)

        _warn_dev_auth(auto_confirm)

        config = DaemonConfig(
            home=home_path,
            poll_interval=poll,
            sync_interval=sync_int,
            port=effective_port,
            consciousness_enabled=not no_consciousness,
        )
        svc = DaemonService(config)

        agent_label = f"[cyan]{agent}[/]" if agent else "[dim]default[/]"
        console.print(f"\n  [green]Starting daemon[/] ({agent_label}) on port [cyan]{effective_port}[/]")
        console.print(f"  Home: {home_path}")
        console.print(f"  Poll: {poll}s | Sync: {sync_int}s")
        consciousness_label = "[red]disabled[/]" if no_consciousness else "[green]enabled[/]"
        console.print(f"  Consciousness: {consciousness_label}")
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
    @click.option("--agent", default=None, help="Named agent to stop.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def daemon_stop(agent: str | None, home: str):
        """Stop the running daemon."""
        from ..daemon import read_pid

        home_path = _resolve_agent_home(agent, home)
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
    @click.option("--agent", default=None, help="Named agent to query.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--port", default=None, type=int, help="API port to query.")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def daemon_status(agent: str | None, home: str, port: int | None, json_out: bool):
        """Show daemon status."""
        from ..daemon import get_daemon_status, is_running, read_pid

        home_path = _resolve_agent_home(agent, home)
        effective_port = _resolve_agent_port(agent, port)
        pid = read_pid(home_path)

        if not is_running(home_path):
            if json_out:
                click.echo(json.dumps({"running": False}))
            else:
                console.print("\n  [yellow]Daemon is not running.[/]\n")
            return

        status = get_daemon_status(home_path, effective_port)
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
                    f"API: [green]http://127.0.0.1:{effective_port}[/]",
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
            console.print(f"  [yellow]API unreachable on port {effective_port}[/]\n")

    @daemon.command("install")
    @click.option("--agent", "agent_name", default=None,
                  help="Agent name for SKCAPSTONE_AGENT (default: from env or 'sovereign').")
    @click.option("--start", is_flag=True, help="Start services immediately after installing.")
    def daemon_install(agent_name: str | None, start: bool):
        """Install the daemon as a system service.

        On Linux: installs systemd user service units.
        On macOS: installs launchd plist files to ~/Library/LaunchAgents/.

        The --agent flag sets the SKCAPSTONE_AGENT environment variable
        in the service definition. If not provided, uses the
        SKCAPSTONE_AGENT env var or defaults to 'sovereign'.

        Examples:

            skcapstone daemon install

            skcapstone daemon install --agent myagent --start
        """
        import platform

        effective_agent = agent_name or os.environ.get("SKCAPSTONE_AGENT", "sovereign")

        if platform.system() == "Darwin":
            from ..launchd import install_service as launchd_install

            console.print(f"\n[cyan]Installing launchd services for agent '{effective_agent}'...[/]")
            result = launchd_install(agent_name=effective_agent, start=start)

            if result["installed"]:
                for svc in result.get("services", []):
                    status = "[green]loaded[/]" if svc.get("loaded") else "[green]installed[/]"
                    console.print(f"  [green]✓[/] {svc['label']} — {status}")
                console.print()
                console.print("[dim]  Manage: launchctl list | grep skcapstone[/]")
                if not start:
                    console.print("[dim]  Start:  launchctl start com.skcapstone.daemon[/]")
                    console.print("[dim]  Or re-run with --start to load immediately.[/]")
            else:
                console.print("[red]Installation failed. Check logs.[/]")
                raise SystemExit(1)
            console.print()

        elif platform.system() == "Linux":
            from ..systemd import install_service, systemd_available

            if not systemd_available():
                console.print("[red]systemd user session not available.[/]")
                console.print("[dim]This command requires a Linux system with systemd.[/]")
                raise SystemExit(1)

            console.print("\n[cyan]Installing skcapstone systemd service...[/]")
            result = install_service(start=start)

            if result["installed"]:
                console.print("[green]  Unit files installed.[/]")
            if result["enabled"]:
                console.print("[green]  Service enabled at login.[/]")
            if result.get("started"):
                console.print("[green]  Service started.[/]")
            console.print()

            if not result["installed"]:
                console.print("[red]Installation failed. Check logs.[/]")
                raise SystemExit(1)
        else:
            console.print(f"[red]Auto-start not supported on {platform.system()}.[/]")
            raise SystemExit(1)

    @daemon.command("uninstall")
    def daemon_uninstall():
        """Uninstall the system service.

        On Linux: stops, disables, and removes systemd unit files.
        On macOS: unloads and removes launchd plist files.

        Examples:

            skcapstone daemon uninstall
        """
        import platform

        if platform.system() == "Darwin":
            from ..launchd import uninstall_service as launchd_uninstall

            console.print("\n[cyan]Uninstalling skcapstone launchd services...[/]")
            result = launchd_uninstall()

            if result["stopped"]:
                console.print("[green]  Services unloaded.[/]")
            if result["removed"]:
                for label in result.get("services", []):
                    console.print(f"  [green]✓[/] Removed {label}")
            console.print()

        elif platform.system() == "Linux":
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

        else:
            console.print(f"[red]Not supported on {platform.system()}.[/]")

    @daemon.command("components")
    @click.option("--agent", default=None, help="Named agent to query.")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--port", default=None, type=int, help="API port to query.")
    @click.option("--json-out", is_flag=True, help="Output as JSON.")
    def daemon_components(agent: str | None, home: str, port: int | None, json_out: bool):
        """Show per-component health (alive/dead/restarting).

        Queries the running daemon for the status of each subsystem:
        poll, health, sync, housekeeping, healing, consciousness,
        scheduler, and heartbeat.

        Examples:

            skcapstone daemon components

            skcapstone daemon components --json-out
        """
        import urllib.error
        import urllib.request

        home_path = _resolve_agent_home(agent, home)
        effective_port = _resolve_agent_port(agent, port)

        try:
            url = f"http://127.0.0.1:{effective_port}/api/v1/components"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            if json_out:
                click.echo(json.dumps({"error": str(exc)}))
            else:
                console.print(f"\n  [red]Cannot reach daemon on port {effective_port}.[/]")
                console.print("  [dim]Is the daemon running? Try: skcapstone daemon start[/]\n")
            return

        if json_out:
            click.echo(json.dumps(data, indent=2))
            return

        components = data.get("components", {})
        if not components:
            console.print("\n  [yellow]No component data returned.[/]\n")
            return

        from rich.table import Table

        table = Table(title="Daemon Components", border_style="dim")
        table.add_column("Component", style="cyan", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Restarts", justify="right")
        table.add_column("Heartbeat age", justify="right")
        table.add_column("Auto-restart", justify="center")
        table.add_column("Last error", style="dim", max_width=40)

        status_colors = {
            "alive": "green",
            "dead": "red",
            "restarting": "yellow",
            "disabled": "dim",
            "pending": "blue",
        }

        for name, info in sorted(components.items()):
            status = info.get("status", "unknown")
            color = status_colors.get(status, "white")
            restarts = str(info.get("restart_count", 0))
            age = info.get("heartbeat_age_seconds")
            age_str = f"{age}s" if age is not None else "—"
            auto = "[green]yes[/]" if info.get("auto_restart") else "[dim]no[/]"
            last_error = (info.get("last_error") or "")[:40] or "—"
            table.add_row(
                name,
                f"[{color}]{status}[/]",
                restarts,
                age_str,
                auto,
                last_error,
            )

        console.print()
        console.print(table)
        console.print()

    @daemon.command("logs")
    @click.option("--lines", "-n", default=50, help="Number of lines (default: 50).")
    @click.option("--follow", "-f", is_flag=True, help="Show the command to follow logs live.")
    def daemon_logs(lines: int, follow: bool):
        """Show daemon logs.

        On Linux: reads from journald.
        On macOS: reads from ~/.skcapstone/logs/ files.

        Examples:

            skcapstone daemon logs

            skcapstone daemon logs -n 100

            skcapstone daemon logs -f
        """
        import platform

        if platform.system() == "Darwin":
            if follow:
                log_path = Path.home() / ".skcapstone" / "logs" / "daemon.stdout.log"
                console.print(f"\n  Run: [bold cyan]tail -f {log_path}[/]\n")
            else:
                from ..launchd import service_logs
                output = service_logs(lines=lines)
                if output.strip():
                    click.echo(output)
                else:
                    console.print("[dim]No logs found in ~/.skcapstone/logs/[/]")
                    console.print("[dim]Is the service installed? Run: skcapstone daemon install[/]")
        else:
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
