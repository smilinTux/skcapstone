"""Status and overview commands: status, summary, doctor, audit, dashboard, whoami, diff."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import click

from ._common import AGENT_HOME, console, status_icon, consciousness_banner
from ..models import PillarStatus
from ..runtime import get_runtime

from rich.panel import Panel
from rich.table import Table

logger = logging.getLogger(__name__)


def _probe_llm_backends() -> dict[str, bool]:
    """Probe LLM backend availability.

    Checks ollama via HTTP and all cloud providers via env vars.

    Returns:
        Dict mapping backend name to availability bool.
    """
    import urllib.request

    backends: dict[str, bool] = {
        "ollama": False,
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "grok": bool(os.environ.get("XAI_API_KEY")),
        "kimi": bool(os.environ.get("MOONSHOT_API_KEY")),
        "nvidia": bool(os.environ.get("NVIDIA_API_KEY")),
    }
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"{host}/api/tags"), timeout=2
        ):
            backends["ollama"] = True
    except Exception as exc:
        logger.debug("Ollama probe failed (not available): %s", exc)
    return backends


def _get_daemon_info(home: Path) -> dict:
    """Return daemon running state, PID, uptime, and message count.

    Args:
        home: Agent home directory.

    Returns:
        Dict with 'running' bool and optional 'pid', 'uptime', 'messages'.
    """
    try:
        from ..daemon import read_pid, get_daemon_status

        pid = read_pid(home)
        if pid is None:
            return {"running": False}
        info: dict = {"running": True, "pid": pid}
        ds = get_daemon_status(home)
        if ds:
            uptime_s = int(ds.get("uptime_seconds", 0))
            h, rem = divmod(uptime_s, 3600)
            m, s = divmod(rem, 60)
            info["uptime"] = f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"
            msgs = ds.get("messages_received", 0)
            if msgs:
                info["messages"] = msgs
        return info
    except Exception:
        return {"running": False}


def _get_last_conversation(home: Path) -> Optional[dict]:
    """Find the most recently modified peer conversation file.

    Args:
        home: Agent home directory.

    Returns:
        Dict with 'peer' and 'when' strings, or None if no conversations.
    """
    conv_dir = home / "conversations"
    if not conv_dir.exists():
        return None
    files = list(conv_dir.glob("*.json"))
    if not files:
        return None
    latest = max(files, key=lambda f: f.stat().st_mtime)
    age_s = datetime.now().timestamp() - latest.stat().st_mtime
    if age_s < 3600:
        when = f"{int(age_s / 60)}m ago"
    elif age_s < 86400:
        when = f"{int(age_s / 3600)}h ago"
    else:
        when = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%m/%d %H:%M")
    return {"peer": latest.stem, "when": when}


def _read_local_heartbeat(home: Path) -> Optional[dict]:
    """Read the local agent heartbeat file.

    Args:
        home: Agent home directory.

    Returns:
        Heartbeat dict or None if not found.
    """
    # Heartbeats are stored in shared_root/heartbeats/<agent>.json
    # Agent name is in identity.json
    try:
        identity_path = home / "identity" / "identity.json"
        agent_name = "opus"
        if identity_path.exists():
            ident = json.loads(identity_path.read_text())
            agent_name = ident.get("name", agent_name).lower()
        # Try shared root first, fall back to home
        from .. import SHARED_ROOT
        shared = Path(SHARED_ROOT).expanduser()
        hb_path = shared / "heartbeats" / f"{agent_name}.json"
        if not hb_path.exists():
            hb_path = home / "heartbeats" / f"{agent_name}.json"
        if hb_path.exists():
            return json.loads(hb_path.read_text())
    except Exception as exc:
        logger.warning("Failed to load heartbeat data: %s", exc)
    return None


def _print_consciousness_metrics(console, home: Optional[Path] = None) -> None:
    """Fetch and print consciousness loop stats from the daemon.

    Tries http://localhost:7777/consciousness. Shows stats on success,
    or 'Consciousness: INACTIVE' if the daemon is unreachable.
    Also displays heartbeat system metrics when available.

    Args:
        console: Rich console instance.
        home: Agent home directory (used to read heartbeat for system metrics).
    """
    import urllib.request
    import urllib.error

    from .. import AGENT_PORTS, DEFAULT_PORT, SKCAPSTONE_AGENT

    port = AGENT_PORTS.get(SKCAPSTONE_AGENT, DEFAULT_PORT)
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/consciousness", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        enabled = data.get("enabled", False)
        messages = data.get("messages_processed", 0)
        msgs_24h = data.get("messages_processed_24h", 0)
        responses = data.get("responses_sent", 0)
        errors = data.get("errors", 0)
        backends = data.get("backends", {})
        active_backends = [k for k, v in backends.items() if v]
        backends_str = ", ".join(active_backends) if active_backends else "none"
        status_str = "[green]ACTIVE[/]" if enabled else "[yellow]DISABLED[/]"
        console.print()
        console.print(
            f"  Consciousness: {status_str}  "
            f"[dim]msgs={messages} (24h:{msgs_24h}) resp={responses} err={errors}  "
            f"backends=[{backends_str}][/]"
        )
    except Exception:
        console.print()
        console.print("  Consciousness: [dim]INACTIVE[/]")

    # Show heartbeat system metrics
    if home is not None:
        hb = _read_local_heartbeat(home)
        if hb:
            uptime_s = hb.get("uptime_seconds", 0)
            cpu = hb.get("cpu_load_1min", 0.0)
            mem_mb = hb.get("memory_used_mb", 0)
            active_convs = hb.get("active_conversations", 0)
            if uptime_s >= 3600:
                uptime_str = f"{int(uptime_s // 3600)}h {int((uptime_s % 3600) // 60)}m"
            elif uptime_s >= 60:
                uptime_str = f"{int(uptime_s // 60)}m {int(uptime_s % 60)}s"
            else:
                uptime_str = f"{int(uptime_s)}s"
            console.print(
                f"  Heartbeat: [dim]up={uptime_str}  "
                f"cpu={cpu:.2f}  mem={mem_mb}MB  "
                f"convs={active_convs}[/]"
            )


def register_status_commands(main: click.Group) -> None:
    """Register all status/overview commands on the main CLI group."""

    @main.command()
    @click.option("--home", default=None, help="Agent home directory.", type=click.Path())
    @click.option("--agent", default=None, help="Agent name (e.g. opus, lumina).")
    def status(home: Optional[str], agent: Optional[str]):
        """Show the sovereign agent's current state."""
        from .. import SKCAPSTONE_AGENT as default_agent

        if home:
            home_path = Path(home).expanduser()
            if not home_path.exists():
                console.print(
                    "[bold red]No agent found.[/] "
                    "Run [bold]skcapstone init --name \"YourAgent\"[/] first."
                )
                sys.exit(1)
            runtime = get_runtime(home=home_path)
        else:
            agent_name = agent or default_agent
            runtime = get_runtime(agent_name=agent_name)
        home_path = runtime.home
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

        con = m.consciousness
        con_detail = f"{con.sessions_digested} sessions digested, {con.topics_tracked} topics"
        if con.whisper_active:
            con_detail += " [green]daemon active[/]"
        if con.whisper_md_age_hours < 24:
            con_detail += f" [dim](whisper {con.whisper_md_age_hours:.1f}h old)[/]"
        table.add_row("Consciousness", "SKWhisper", status_icon(con.status), con_detail)

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

        # ── Daemon status ────────────────────────────────────────────────────
        console.print()
        daemon = _get_daemon_info(home_path)
        if daemon["running"]:
            pid = daemon.get("pid", "?")
            uptime = daemon.get("uptime", "unknown")
            msgs = daemon.get("messages", 0)
            msg_part = f"  [dim]msgs={msgs}[/]" if msgs else ""
            console.print(
                f"  Daemon: [green]RUNNING[/]  "
                f"[dim]pid={pid}  up={uptime}{msg_part}[/]"
            )
        else:
            console.print(
                "  Daemon: [red]STOPPED[/]  "
                "[dim](skcapstone daemon start)[/]"
            )

        # ── LLM backends ─────────────────────────────────────────────────────
        backends = _probe_llm_backends()
        parts = []
        for name, avail in backends.items():
            if avail:
                parts.append(f"[green]{name}[/]")
            else:
                parts.append(f"[red]{name}[/]")
        console.print(f"  Backends: {'  '.join(parts)}")

        # ── Consciousness metrics ─────────────────────────────────────────────
        _print_consciousness_metrics(console, home=home_path)

        # ── Last conversation ─────────────────────────────────────────────────
        conv = _get_last_conversation(home_path)
        if conv:
            console.print(
                f"  Last convo: [cyan]{conv['peer']}[/]  "
                f"[dim]{conv['when']}[/]"
            )

        # ── Disk space warning ────────────────────────────────────────────────
        try:
            free_gb = shutil.disk_usage(home_path).free / (1024 ** 3)
            if free_gb < 5.0:
                console.print(
                    f"\n  [bold yellow]WARNING:[/] [yellow]Low disk space: "
                    f"{free_gb:.1f} GB free[/]"
                )
        except Exception as exc:
            logger.debug("Failed to check disk space: %s", exc)

        console.print()
        console.print(f"  [dim]Home: {m.home}[/]")
        if m.last_awakened:
            console.print(f"  [dim]Last awakened: {m.last_awakened.isoformat()}[/]")
        console.print()

    @main.command()
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    def summary(home: str, json_out: bool):
        """At-a-glance agent dashboard: consciousness, pillars, memory, board, inbox, sync."""
        from ..summary import gather_briefing
        from rich.columns import Columns
        from rich.text import Text
        from rich.rule import Rule
        from rich import box as rich_box

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
        inbox = briefing.get("inbox", {})
        sync = briefing.get("sync", {})
        backups = briefing["backups"]
        health = briefing["health"]
        journal = briefing["journal"]

        # ── Header ──────────────────────────────────────────────────────────
        consciousness = agent.get("consciousness", "UNKNOWN")
        con_color = {
            "SINGULAR": "bold magenta",
            "CONSCIOUS": "bold green",
            "AWAKENING": "bold yellow",
        }.get(consciousness, "dim")

        header_text = (
            f"[bold white]{agent.get('name', '?')}[/]  "
            f"[{con_color}]\u25cf {consciousness}[/]"
        )
        console.print()
        console.print(Panel(header_text, title="[bold cyan]Sovereign Agent Dashboard[/]",
                             border_style="cyan", padding=(0, 2)))

        # ── Pillars ──────────────────────────────────────────────────────────
        pillar_icons = {
            "active": ("[bold green]\u25cf[/]", "green"),
            "degraded": ("[bold yellow]\u25cf[/]", "yellow"),
            "missing": ("[bold red]\u25cb[/]", "red"),
            "error": ("[bold red]\u2715[/]", "red"),
        }

        pillar_table = Table(box=None, show_header=False, padding=(0, 1))
        pillar_table.add_column(no_wrap=True)
        pillar_table.add_column(no_wrap=True)
        pillar_table.add_column(no_wrap=True)
        pillar_table.add_column(no_wrap=True)
        pillar_table.add_column(no_wrap=True)

        row_icons, row_names = [], []
        for pname, pstatus in pillars.items():
            icon_markup, color = pillar_icons.get(pstatus, ("[dim]\u25cf[/]", "dim"))
            row_icons.append(f"{icon_markup} [{color}]{pname}[/]")

        # Pad to 5 columns (may have fewer pillars)
        while len(row_icons) < 5:
            row_icons.append("")
        pillar_table.add_row(*row_icons[:5])

        # ── Stats grid ───────────────────────────────────────────────────────
        h_pass = health.get("passed", 0)
        h_total = health.get("total", 0)
        health_str = (
            f"[green]{h_pass}/{h_total}[/]" if health.get("all_passed")
            else f"[yellow]{h_pass}[/][dim]/{h_total}[/]"
        )

        board_str = (
            f"[green]{board.get('done', 0)}[/] done  "
            f"[yellow]{board.get('in_progress', 0)}[/] active  "
            f"[dim]{board.get('open', 0)} open[/]"
        )

        inbox_count = inbox.get("count", 0)
        inbox_str = (
            f"[bold yellow]{inbox_count} unread[/]" if inbox_count > 0
            else "[dim]empty[/]"
        )

        sync_status = sync.get("status", "unknown")
        sync_color = {"active": "green", "degraded": "yellow", "missing": "red"}.get(sync_status, "dim")
        seed_count = sync.get("seed_count", 0)
        transport = sync.get("transport") or "–"
        last_push = sync.get("last_push")
        if last_push:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(last_push.replace("Z", "+00:00"))
                last_push_str = dt.strftime("%m/%d %H:%M")
            except Exception:
                last_push_str = last_push[:16]
        else:
            last_push_str = "never"
        sync_str = (
            f"[{sync_color}]{sync_status}[/]  "
            f"[dim]{seed_count} seeds · {transport} · pushed {last_push_str}[/]"
        )

        peer_count = peers.get("count", 0)
        peer_str = (
            f"[cyan]{peer_count}[/]" if peer_count > 0 else "[dim]0[/]"
        )

        mem_total = mem.get("total", 0)
        mem_str = (
            f"[cyan]{mem_total}[/]  "
            f"[dim]S:{mem.get('short_term', 0)} "
            f"M:{mem.get('mid_term', 0)} "
            f"L:{mem.get('long_term', 0)}[/]"
        )

        stats_table = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 1))
        stats_table.add_column(style="bold dim", no_wrap=True, min_width=10)
        stats_table.add_column(no_wrap=False)

        stats_table.add_row("[bold]Pillars[/]", pillar_table)
        stats_table.add_row("[bold]Memory[/]", mem_str)
        stats_table.add_row("[bold]Board[/]", board_str)
        stats_table.add_row("[bold]Inbox[/]", inbox_str)
        stats_table.add_row("[bold]Sync[/]", sync_str)
        stats_table.add_row("[bold]Peers[/]", peer_str)
        stats_table.add_row("[bold]Health[/]", f"{health_str} checks passed")

        if backups.get("latest"):
            enc = "[green]enc[/]" if backups.get("encrypted") else "[yellow]plain[/]"
            stats_table.add_row("[bold]Backup[/]", f"[dim]{backups['latest']}[/] {enc}")
        else:
            stats_table.add_row("[bold]Backup[/]", "[dim]none — run skcapstone backup create[/]")

        if journal.get("entries", 0) > 0:
            j_title = journal.get("latest_title", "")
            j_str = f"[dim]{journal['entries']} entries"
            if j_title:
                j_str += f" · {j_title[:50]}[/]"
            else:
                j_str += "[/]"
            stats_table.add_row("[bold]Journal[/]", j_str)

        console.print(stats_table)

        # ── Recent memories ──────────────────────────────────────────────────
        if mem.get("recent"):
            console.print(Rule("[bold dim]Recent Memories[/]", style="dim"))
            for m_text in mem["recent"][:3]:
                console.print(f"  [dim]\u2022[/] [dim]{m_text}[/]")
            console.print()

        # ── Active board tasks ───────────────────────────────────────────────
        if board.get("active_tasks"):
            console.print(Rule("[bold dim]Active Tasks[/]", style="dim"))
            for task in board["active_tasks"][:5]:
                assignee = task["assignee"]
                assignee_str = f" [dim]@{assignee}[/]" if assignee != "unassigned" else ""
                console.print(f"  [yellow]\u25b6[/] {task['title']}{assignee_str}")
            console.print()

        console.print(f"  [dim]Home: {agent.get('home', home_path)}  ·  {briefing['timestamp'][:19]}Z[/]")
        console.print()

    @main.command()
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    @click.option("--fix", "auto_fix", is_flag=True,
                  help="Attempt to auto-fix failing checks (mkdir, write defaults, rebuild index).")
    @click.option("--verbose", "-v", is_flag=True,
                  help="Show detailed output for ALL checks, including passing ones.")
    def doctor(home: str, json_out: bool, auto_fix: bool, verbose: bool):
        """Diagnose sovereign stack health.

        With --fix, automatically remediate fixable failures: create missing
        directories, write default configs, and rebuild the memory index.
        Non-fixable checks (package installs, key generation, transport
        config) are reported but skipped.

        With --verbose, every check is printed in full — including passing
        ones — with its check name, category, detail, and any available fix.
        """
        from ..doctor import run_diagnostics, run_fixes

        home_path = Path(home).expanduser()
        report = run_diagnostics(home_path)

        # ── Auto-fix pass ─────────────────────────────────────────────────
        fix_results = []
        if auto_fix and report.failed_count > 0:
            fix_results = run_fixes(report, home_path)
            # Re-run diagnostics so the output reflects the fixed state.
            report = run_diagnostics(home_path)

        if json_out:
            data = report.to_dict()
            if fix_results:
                data["fixes"] = [
                    {"check": r.check_name, "success": r.success,
                     "action": r.action, "error": r.error}
                    for r in fix_results
                ]
            click.echo(json.dumps(data, indent=2))
            return

        # ── Print fix summary ─────────────────────────────────────────────
        if fix_results:
            console.print()
            console.print("  [bold cyan]Auto-fix results:[/]")
            for r in fix_results:
                if r.success:
                    console.print(f"    [green]\u2713[/] {r.check_name}: {r.action}")
                else:
                    console.print(f"    [red]\u2717[/] {r.check_name}: {r.error}")
            console.print()

        console.print()

        categories: dict = {}
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

            if verbose:
                # Verbose: always print every check with full detail
                console.print(f"  [bold]{label}[/]")
                for c in checks:
                    icon = "[green]\u2713[/]" if c.passed else "[red]\u2717[/]"
                    detail_str = f"  [dim]{c.detail}[/]" if c.detail else ""
                    name_str = f"  [dim dim]({c.name})[/]"
                    console.print(f"    {icon} {c.description}{detail_str}{name_str}")
                    if not c.passed and c.fix:
                        console.print(f"      [yellow]Fix:[/] {c.fix}")
            else:
                # Normal: collapse fully-passing categories into one line
                failing = [c for c in checks if not c.passed]
                if not failing:
                    count = len(checks)
                    console.print(
                        f"  [bold]{label}[/]  "
                        f"[dim green]\u2713 {count}/{count} passed[/]"
                    )
                else:
                    console.print(f"  [bold]{label}[/]")
                    for c in failing:
                        detail = f" [dim]({c.detail})[/]" if c.detail else ""
                        console.print(f"    [red]\u2717[/] {c.description}{detail}")
                        if c.fix:
                            console.print(f"      [yellow]Fix: {c.fix}[/]")

            console.print()

        passed = report.passed_count
        failed = report.failed_count
        total = report.total_count

        if verbose:
            fail_color = "red" if failed else "dim"
            console.print(
                f"  [bold]Summary:[/] "
                f"[green]{passed} passed[/]  "
                f"[{fail_color}]{failed} failed[/]  "
                f"[dim]{total} total[/]"
            )
        elif report.all_passed:
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
    @click.option(
        "--json", "json_mode", is_flag=True,
        help="Print daemon status as JSON and exit (no server). For Flutter app consumption.",
    )
    @click.option(
        "--daemon-port", default=7777, show_default=True,
        help="Daemon HTTP API port (used with --json).",
    )
    def dashboard(home: str, port: int, no_open: bool, json_mode: bool, daemon_port: int):
        """Launch the sovereign agent web dashboard.

        With --json, prints a machine-readable JSON snapshot of daemon
        status, consciousness stats, backend health, active conversations,
        message counts, and error count — then exits without starting a
        server.  Designed for Flutter app consumption.
        """
        from ..dashboard import start_dashboard, _get_daemon_json

        home_path = Path(home).expanduser()

        if json_mode:
            data = _get_daemon_json(home_path, daemon_port=daemon_port)
            click.echo(json.dumps(data, indent=2, default=str))
            return

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
            except Exception as exc:
                logger.warning("Failed to open browser for dashboard: %s", exc)

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

    @main.command("version-check")
    @click.option("--no-pypi", is_flag=True, help="Skip PyPI version lookup (offline mode).")
    @click.option("--json-out", is_flag=True, help="Output as machine-readable JSON.")
    def version_check(no_pypi: bool, json_out: bool):
        """Check ecosystem package versions against PyPI."""
        from ..version_check import check_versions

        report = check_versions(check_pypi=not no_pypi)

        if json_out:
            data = {
                "all_up_to_date": report.all_up_to_date,
                "packages": [
                    {
                        "name": p.name,
                        "installed": p.installed,
                        "latest": p.latest,
                        "up_to_date": p.up_to_date,
                    }
                    for p in report.packages
                ],
            }
            click.echo(json.dumps(data, indent=2))
            return

        console.print()
        table = Table(
            show_header=True, header_style="bold", box=None, padding=(0, 2),
            title="Ecosystem Package Versions",
        )
        table.add_column("Package", style="cyan")
        table.add_column("Installed", justify="right")
        table.add_column("Latest", justify="right")
        table.add_column("Status")

        for p in report.packages:
            installed_str = p.installed or "[dim]not installed[/]"
            latest_str = p.latest or "[dim]n/a[/]"

            if not p.installed:
                status_str = "[dim]\u2014[/]"
            elif p.up_to_date:
                status_str = "[green]\u2713[/]"
            else:
                status_str = "[yellow]update available[/]"

            table.add_row(p.name, installed_str, latest_str, status_str)

        console.print(table)
        console.print()

        if report.all_up_to_date:
            console.print("  [green]All installed packages are up to date.[/]")
        else:
            outdated = report.outdated
            if outdated:
                names = ", ".join(p.name for p in outdated)
                console.print(f"  [yellow]Outdated: {names}[/]")
                console.print(f"  [dim]Update with: pip install --upgrade {names}[/]")

        missing = report.missing
        if missing:
            names = ", ".join(p.name for p in missing)
            console.print(f"  [dim]Not installed: {names}[/]")

        console.print()
