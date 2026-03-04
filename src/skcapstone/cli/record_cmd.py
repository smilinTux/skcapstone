"""Session record/replay commands.

    skcapstone record [--output FILE.jsonl]
        Start the MCP server in recording mode.  All tool calls +
        responses are captured as JSONL.  Sessions are also auto-saved
        to ~/.skcapstone/sessions/ (last 5 kept).

    skcapstone replay FILE.jsonl [--dry-run]
        Play back a recorded session.  --dry-run prints what would
        be called without executing any handlers.

    skcapstone sessions list
        List all auto-saved sessions.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console


def register_record_commands(main: click.Group) -> None:
    """Register record, replay, and sessions commands."""

    # ------------------------------------------------------------------
    # skcapstone record
    # ------------------------------------------------------------------

    @main.command("record")
    @click.option(
        "--output", "-o",
        default=None,
        type=click.Path(),
        help="Write tool calls to this JSONL file (in addition to auto-session).",
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def record(output: str | None, home: str) -> None:
        """Start MCP server in recording mode.

        Every tool call and its response is captured as a JSONL line.
        Sessions are also auto-saved to ~/.skcapstone/sessions/ with
        the last 5 retained.

        \b
        Examples:
            skcapstone record
            skcapstone record --output /tmp/debug.jsonl
            SKCAPSTONE_RECORD_FILE=/tmp/debug.jsonl skcapstone mcp serve
        """
        if output:
            os.environ["SKCAPSTONE_RECORD_FILE"] = str(Path(output).expanduser())
            console.print(f"  [green]Recording to:[/] {output}")

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print(
                "[bold red]No agent found.[/] Run [cyan]skcapstone init[/] first."
            )
            sys.exit(1)

        console.print(
            "  [dim]Auto-session saved to[/] "
            f"[cyan]{home_path / 'sessions'}[/]  (last 5 kept)"
        )
        console.print("  [dim]Starting MCP server … (Ctrl-C to stop)[/]\n")

        import asyncio
        from ..mcp_server import _run_server
        try:
            asyncio.run(_run_server())
        except KeyboardInterrupt:
            console.print("\n  [yellow]Recording stopped.[/]")

    # ------------------------------------------------------------------
    # skcapstone replay
    # ------------------------------------------------------------------

    @main.command("replay")
    @click.argument("session_file", type=click.Path(exists=True))
    @click.option(
        "--dry-run", "dry_run",
        is_flag=True,
        default=False,
        help="Print what would be called without executing handlers.",
    )
    @click.option(
        "--format", "fmt",
        type=click.Choice(["text", "json"]),
        default="text",
        help="Output format.",
    )
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def replay(
        session_file: str,
        dry_run: bool,
        fmt: str,
        home: str,
    ) -> None:
        """Replay a recorded JSONL session.

        \b
        Modes:
            --dry-run   Print tool names + arguments only (no execution).
            (default)   Execute each tool call against the live handlers
                        and compare results to the recording.

        \b
        Examples:
            skcapstone replay session-20260302T120000.jsonl --dry-run
            skcapstone replay /tmp/debug.jsonl
            skcapstone replay /tmp/debug.jsonl --format json
        """
        from ..session_replayer import SessionReplayer

        path = Path(session_file).expanduser()
        replayer = SessionReplayer(path, dry_run=dry_run)
        results = list(replayer.replay())

        if fmt == "json":
            # Pure JSON output — no decorative header so callers can parse stdout.
            rows = []
            for r in results:
                rows.append({
                    "index": r.index,
                    "tool": r.tool,
                    "arguments": r.arguments,
                    "recorded_result": r.recorded_result,
                    "replayed_result": r.replayed_result,
                    "duration_ms": r.duration_ms,
                    "match": r.match,
                    "error": r.error,
                })
            click.echo(json.dumps(rows, indent=2, default=str))
            return

        # Text output
        mode_label = "[yellow]DRY-RUN[/]" if dry_run else "[green]LIVE[/]"
        console.print(f"\n  Replaying [bold]{path.name}[/]  {mode_label}\n")
        if not results:
            console.print("  [dim]Session file is empty.[/]\n")
            return

        mismatches = 0
        errors = 0
        for r in results:
            args_preview = json.dumps(r.arguments, ensure_ascii=False)
            if len(args_preview) > 80:
                args_preview = args_preview[:77] + "..."

            if dry_run:
                status = "[dim]SKIP[/]"
            elif r.error:
                status = "[red]ERROR[/]"
                errors += 1
            elif r.match is True:
                status = "[green]MATCH[/]"
            elif r.match is False:
                status = "[yellow]DIFF[/]"
                mismatches += 1
            else:
                status = "[dim]?[/]"

            console.print(
                f"  [{r.index:>3}] {status}  [cyan]{r.tool}[/]"
                f"  [dim]{args_preview}[/]"
                f"  [dim]{r.duration_ms}ms[/]"
            )
            if r.error:
                console.print(f"        [red]{r.error}[/]")

        total = len(results)
        console.print(f"\n  {total} call(s) replayed", end="")
        if not dry_run:
            if mismatches:
                console.print(f"  [yellow]{mismatches} diff(s)[/]", end="")
            if errors:
                console.print(f"  [red]{errors} error(s)[/]", end="")
        console.print("\n")

        if not dry_run and (mismatches or errors):
            sys.exit(1)

    # ------------------------------------------------------------------
    # skcapstone sessions
    # ------------------------------------------------------------------

    @main.group("sessions")
    def sessions_group() -> None:
        """Manage auto-saved MCP sessions."""

    @sessions_group.command("list")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--limit", default=10, help="Max sessions to show.")
    def sessions_list(home: str, limit: int) -> None:
        """List auto-saved session files newest-first.

        \b
        Example:
            skcapstone sessions list
            skcapstone sessions list --limit 3
        """
        from ..session_recorder import list_sessions, load_session

        home_path = Path(home).expanduser()
        files = list_sessions(home_path)

        if not files:
            console.print("\n  [dim]No sessions found in "
                          f"{home_path / 'sessions'}[/]\n")
            return

        console.print(f"\n  [bold]{len(files)}[/] session(s) found "
                      f"(showing up to {limit}):\n")
        for f in files[:limit]:
            try:
                entries = load_session(f)
                count = len(entries)
                tools = list({e.get("tool", "?") for e in entries})
                tools_str = ", ".join(sorted(tools)[:5])
                if len(tools) > 5:
                    tools_str += f" +{len(tools) - 5}"
            except Exception:
                count = 0
                tools_str = "[dim]unreadable[/]"

            size_kb = f.stat().st_size / 1024
            console.print(
                f"  [cyan]{f.name}[/]  "
                f"[dim]{count} calls  {size_kb:.1f}KB  {tools_str}[/]"
            )
            console.print(f"    [dim]{f}[/]")
        console.print()
