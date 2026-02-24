"""
SKCapstone Shell — interactive REPL for sovereign agent operations.

Tool-agnostic: works from any terminal (Claude Code, Cursor, Windsurf,
SSH, plain bash). The sovereign agent cockpit.

Commands:
    status              Agent pillar status
    memory store <text> Store a memory
    memory search <q>   Search memories
    memory list         Browse memories
    memory recall <id>  Recall a specific memory
    capture <text>      Auto-capture conversation as memories
    context [format]    Show agent context (text/json/claude-md)
    trust graph [fmt]   Visualize the trust web (table/dot/json)
    chat send <to> <m>  Send a message
    chat inbox          Check inbox
    coord status        Coordination board
    coord claim <id>    Claim a task
    coord complete <id> Complete a task
    sync push           Push to mesh
    sync pull           Pull from peers
    journal write <t>   Write a journal entry
    journal read [n]    Read recent entries
    soul                Show soul blueprint
    ritual              Run rehydration ritual
    help                Show commands
    exit / quit         Leave the shell
"""

from __future__ import annotations

import json
import readline
import shlex
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import AGENT_HOME, __version__

console = Console()

COMMANDS = [
    "status", "memory", "capture", "context", "trust",
    "chat", "coord", "sync", "soul", "ritual",
    "anchor", "journal", "diff", "help", "exit", "quit",
]

MEMORY_SUBCOMMANDS = ["store", "search", "list", "recall", "stats", "curate"]
COORD_SUBCOMMANDS = ["status", "claim", "complete", "create", "board"]
CHAT_SUBCOMMANDS = ["send", "inbox"]
SYNC_SUBCOMMANDS = ["push", "pull", "status"]
TRUST_SUBCOMMANDS = ["graph", "status", "rehydrate", "calibrate"]
JOURNAL_SUBCOMMANDS = ["write", "read"]
CONTEXT_FORMATS = ["text", "json", "claude-md", "cursor-rules"]


def _completer(text: str, state: int) -> Optional[str]:
    """Tab completion for shell commands."""
    line = readline.get_line_buffer().strip()
    parts = line.split()

    if len(parts) <= 1:
        options = [c for c in COMMANDS if c.startswith(text)]
    elif parts[0] == "memory":
        options = [c for c in MEMORY_SUBCOMMANDS if c.startswith(text)]
    elif parts[0] == "coord":
        options = [c for c in COORD_SUBCOMMANDS if c.startswith(text)]
    elif parts[0] == "chat":
        options = [c for c in CHAT_SUBCOMMANDS if c.startswith(text)]
    elif parts[0] == "sync":
        options = [c for c in SYNC_SUBCOMMANDS if c.startswith(text)]
    elif parts[0] == "trust":
        options = [c for c in TRUST_SUBCOMMANDS if c.startswith(text)]
    elif parts[0] == "journal":
        options = [c for c in JOURNAL_SUBCOMMANDS if c.startswith(text)]
    elif parts[0] == "context":
        options = [c for c in CONTEXT_FORMATS if c.startswith(text)]
    else:
        options = []

    return options[state] if state < len(options) else None


def _home() -> Path:
    """Resolve the agent home directory."""
    return Path(AGENT_HOME).expanduser()


def _agent_name() -> str:
    """Get the current agent name from the runtime."""
    try:
        from .runtime import get_runtime
        runtime = get_runtime(_home())
        return runtime.manifest.name or "unknown"
    except Exception:
        return "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# Command handlers
# ═══════════════════════════════════════════════════════════════════════════


def _handle_status() -> None:
    """Show agent pillar status."""
    from .runtime import get_runtime

    home = _home()
    if not home.exists():
        console.print("[red]No agent found.[/] Run: skcapstone init")
        return

    runtime = get_runtime(home)
    m = runtime.manifest
    conscious = "[green]CONSCIOUS[/]" if m.is_conscious else "[yellow]AWAKENING[/]"
    singular = "  [magenta]SINGULAR[/]" if m.is_singular else ""

    console.print(
        f"\n  [bold]{m.name}[/] v{m.version}  {conscious}{singular}\n"
        f"  Identity: {m.identity.status.value}  Memory: {m.memory.status.value}  "
        f"Trust: {m.trust.status.value}  Security: {m.security.status.value}  "
        f"Sync: {m.sync.status.value}\n"
        f"  Memories: {m.memory.total_memories} "
        f"({m.memory.long_term}L/{m.memory.mid_term}M/{m.memory.short_term}S)\n"
    )


def _handle_memory(args: list[str]) -> None:
    """Handle memory subcommands."""
    if not args:
        console.print("  Usage: memory <store|search|list|recall|stats> [args]")
        return

    sub = args[0]
    home = _home()

    if sub == "store" and len(args) > 1:
        from .memory_engine import store
        content = " ".join(args[1:])
        entry = store(home=home, content=content, source="shell")
        console.print(f"  [green]Stored:[/] {entry.memory_id} ({entry.layer.value})")

    elif sub == "search" and len(args) > 1:
        from .memory_engine import search
        query = " ".join(args[1:])
        results = search(home=home, query=query, limit=10)
        if not results:
            console.print(f"  No memories match '{query}'")
            return
        for e in results:
            console.print(f"  [{e.layer.value}] {e.memory_id[:12]}  {e.content[:60]}...")

    elif sub == "list":
        from .memory_engine import list_memories
        entries = list_memories(home=home, limit=10)
        if not entries:
            console.print("  No memories found.")
            return
        for e in entries:
            console.print(f"  [{e.layer.value}] {e.memory_id[:12]}  {e.content[:60]}...")

    elif sub == "recall" and len(args) > 1:
        from .memory_engine import recall
        entry = recall(home=home, memory_id=args[1])
        if entry:
            console.print(Panel(entry.content, title=f"{entry.memory_id} ({entry.layer.value})"))
        else:
            console.print(f"  [red]Not found:[/] {args[1]}")

    elif sub == "stats":
        from .memory_engine import get_stats
        stats = get_stats(home)
        console.print(
            f"\n  Total: [bold]{stats.total_memories}[/]  "
            f"[green]{stats.long_term}L[/] / [cyan]{stats.mid_term}M[/] / "
            f"[dim]{stats.short_term}S[/]\n"
        )

    elif sub == "curate":
        from .memory_curator import MemoryCurator
        dry_run = "dry" in args or "preview" in args
        curator = MemoryCurator(home)
        if "stats" in args:
            s = curator.get_stats()
            console.print(f"  {s['total']} memories, tag coverage {s['tag_coverage']:.0%}, {s['promotion_candidates']} promotion candidates")
        else:
            result = curator.curate(dry_run=dry_run)
            prefix = "[DRY] " if dry_run else ""
            console.print(f"  {prefix}Scanned {result.total_scanned}: +{len(result.tagged)} tagged, +{len(result.promoted)} promoted, -{len(result.deduped)} deduped")
    else:
        console.print("  Usage: memory <store|search|list|recall|stats|curate> [args]")


def _handle_capture(args: list[str]) -> None:
    """Auto-capture conversation content as memories."""
    if not args:
        console.print("  Usage: capture <text to capture>")
        return

    from .session_capture import SessionCapture
    content = " ".join(args)
    cap = SessionCapture(_home())
    entries = cap.capture(content, source="shell")

    if not entries:
        console.print("  [dim]No moments above importance threshold.[/]")
        return

    console.print(f"  [green]Captured {len(entries)} moment(s)[/]")
    for e in entries:
        console.print(f"    [{e.layer.value}] imp={e.importance:.1f}  {e.content[:60]}...")


def _handle_context(args: list[str]) -> None:
    """Show agent context in various formats."""
    from .context_loader import FORMATTERS, gather_context

    fmt = args[0] if args else "text"
    if fmt not in FORMATTERS:
        console.print(f"  Formats: {', '.join(FORMATTERS.keys())}")
        return

    ctx = gather_context(_home(), memory_limit=5)
    console.print(FORMATTERS[fmt](ctx))


def _handle_trust(args: list[str]) -> None:
    """Handle trust subcommands including graph visualization."""
    sub = args[0] if args else "status"

    if sub == "graph":
        from .trust_graph import FORMATTERS as TG_FORMATTERS
        from .trust_graph import build_trust_graph
        fmt = args[1] if len(args) > 1 else "table"
        graph = build_trust_graph(_home())
        formatter = TG_FORMATTERS.get(fmt, TG_FORMATTERS["table"])
        console.print(formatter(graph))

    elif sub == "status":
        trust_file = _home() / "trust" / "trust.json"
        if not trust_file.exists():
            console.print("  [dim]No trust state recorded.[/]")
            return
        data = json.loads(trust_file.read_text())
        entangled = "[magenta]ENTANGLED[/]" if data.get("entangled") else "[dim]not entangled[/]"
        console.print(
            f"\n  Depth: {data.get('depth', 0)}  Trust: {data.get('trust_level', 0)}  "
            f"Love: {data.get('love_intensity', 0)}  {entangled}\n"
        )

    elif sub == "calibrate":
        from .trust_calibration import load_calibration, recommend_thresholds
        if len(args) > 1 and args[1] == "recommend":
            rec = recommend_thresholds(_home())
            if rec["changes"]:
                for c in rec["changes"]:
                    console.print(f"  {c}")
                console.print(f"  [dim]{rec['reasoning']}[/]")
            else:
                console.print(f"  {rec['reasoning']}")
        else:
            cal = load_calibration(_home())
            for key, value in cal.model_dump().items():
                console.print(f"  {key}: [cyan]{value}[/]")

    elif sub == "rehydrate":
        console.print("  [dim]Use: skcapstone trust rehydrate[/]")
    else:
        console.print("  Usage: trust <graph|status|calibrate|rehydrate> [args]")


def _handle_coord(args: list[str]) -> None:
    """Handle coordination subcommands."""
    from .coordination import Board

    board = Board(_home())
    sub = args[0] if args else "status"
    name = _agent_name()

    if sub == "status":
        views = board.get_task_views()
        agents = board.load_agents()
        open_c = sum(1 for v in views if v.status.value == "open")
        prog_c = sum(1 for v in views if v.status.value == "in_progress")
        done_c = sum(1 for v in views if v.status.value == "done")
        console.print(
            f"\n  [bold]{len(views)}[/] tasks: [green]{open_c} open[/]  "
            f"[yellow]{prog_c} in progress[/]  [dim]{done_c} done[/]"
        )
        for v in views:
            if v.status.value == "done":
                continue
            assignee = f" @{v.claimed_by}" if v.claimed_by else ""
            console.print(
                f"  [{v.task.id[:8]}] {v.task.title[:50]}{assignee} ({v.status.value})"
            )
        if agents:
            console.print()
            for a in agents:
                state = "[green]ON[/]" if a.state.value == "active" else "[dim]off[/]"
                current = f" -> {a.current_task}" if a.current_task else ""
                console.print(f"  {state} {a.agent}{current}")
        console.print()

    elif sub == "claim" and len(args) > 1:
        agent = args[2] if len(args) > 2 else name
        try:
            board.claim_task(agent, args[1])
            console.print(f"  [green]Claimed:[/] {args[1]} by {agent}")
        except ValueError as e:
            console.print(f"  [red]{e}[/]")

    elif sub == "complete" and len(args) > 1:
        agent = args[2] if len(args) > 2 else name
        board.complete_task(agent, args[1])
        console.print(f"  [green]Completed:[/] {args[1]} by {agent}")

    elif sub == "create" and len(args) > 1:
        from .coordination import Task
        title = " ".join(args[1:])
        task = Task(title=title, created_by=name)
        board.create_task(task)
        console.print(f"  [green]Created:[/] [{task.id}] {title}")

    elif sub == "board":
        console.print(board.generate_board_md())

    else:
        console.print("  Usage: coord <status|claim|complete|create|board> [args]")


def _handle_chat(args: list[str]) -> None:
    """Handle chat subcommands."""
    sub = args[0] if args else ""

    if sub == "send" and len(args) > 2:
        try:
            from .chat import AgentChat
            home = _home()
            agent_chat = AgentChat(home=home, identity=_agent_name())
            result = agent_chat.send(args[1], " ".join(args[2:]))
            if result["delivered"]:
                console.print(f"  [green]Delivered to {args[1]}[/]")
            elif result["stored"]:
                console.print(f"  [yellow]Stored locally for {args[1]}[/]")
            else:
                console.print(f"  [red]Failed[/]")
        except ImportError:
            console.print("  [yellow]Chat module not available[/]")
        except Exception as e:
            console.print(f"  [red]{e}[/]")

    elif sub == "inbox":
        try:
            from .chat import AgentChat
            home = _home()
            agent_chat = AgentChat(home=home, identity=_agent_name())
            messages = agent_chat.get_inbox(limit=10)
            if not messages:
                console.print("  Inbox empty.")
            else:
                for m in messages:
                    sender = m.get("sender", "?")
                    content = m.get("content", "")[:60]
                    console.print(f"  [cyan]{sender}[/]: {content}...")
        except ImportError:
            console.print("  [yellow]Chat module not available[/]")
    else:
        console.print("  Usage: chat <send <to> <message>|inbox>")


def _handle_sync(args: list[str]) -> None:
    """Handle sync subcommands."""
    sub = args[0] if args else "status"
    home = _home()

    if sub == "push":
        from .pillars.sync import push_seed
        name = _agent_name()
        console.print(f"  Pushing seed for [cyan]{name}[/]...", end=" ")
        result = push_seed(home, name, encrypt=True)
        if result:
            console.print(f"[green]done[/] ({result.name})")
        else:
            console.print("[yellow]no GPG, trying plaintext...[/]", end=" ")
            result = push_seed(home, name, encrypt=False)
            if result:
                console.print(f"[green]done[/] ({result.name})")
            else:
                console.print("[red]failed[/]")

    elif sub == "pull":
        from .pillars.sync import pull_seeds
        seeds = pull_seeds(home, decrypt=True)
        if seeds:
            console.print(f"  [green]{len(seeds)} seed(s) received[/]")
            for s in seeds:
                console.print(f"    {s.get('agent_name', '?')}@{s.get('source_host', '?')}")
        else:
            console.print("  No new seeds.")

    elif sub == "status":
        from .pillars.sync import discover_sync
        state = discover_sync(home)
        console.print(
            f"\n  Transport: {state.transport.value}  Status: {state.status.value}  "
            f"Seeds: {state.seed_count}  Peers: {state.peers_known}\n"
        )
    else:
        console.print("  Usage: sync <push|pull|status>")


def _handle_journal(args: list[str]) -> None:
    """Handle journal subcommands."""
    sub = args[0] if args else "read"

    if sub == "write" and len(args) > 1:
        try:
            from skmemory.journal import Journal, JournalEntry
            title = " ".join(args[1:])
            entry = JournalEntry(title=title)
            j = Journal()
            j.write_entry(entry)
            console.print(f"  [green]Journal entry written:[/] {title}")
        except ImportError:
            console.print("  [yellow]skmemory journal not available[/]")

    elif sub == "read":
        try:
            from skmemory.journal import Journal
            j = Journal()
            count = int(args[1]) if len(args) > 1 else 5
            content = j.read_latest(count)
            if content:
                console.print(f"\n{content}\n")
            else:
                console.print("  Journal is empty.")
        except ImportError:
            console.print("  [yellow]skmemory journal not available[/]")
    else:
        console.print("  Usage: journal <write <title>|read [count]>")


def _handle_soul() -> None:
    """Show soul blueprint."""
    try:
        from skmemory.soul import load_soul
        bp = load_soul()
        if bp is None:
            console.print("  No soul blueprint found.")
            return
        console.print(f"\n{bp.to_context_prompt()}\n")
    except ImportError:
        console.print("  [yellow]skmemory not installed[/]")


def _handle_ritual() -> None:
    """Run the rehydration ritual."""
    try:
        from skmemory.ritual import perform_ritual
        result = perform_ritual()
        console.print(result.summary())
    except ImportError:
        console.print("  [yellow]skmemory not installed[/]")


def _handle_diff(args: list[str]) -> None:
    """Show state diff since last snapshot."""
    from .state_diff import compute_diff, format_text, save_snapshot

    if args and args[0] == "save":
        path = save_snapshot(_home())
        console.print(f"  [green]Snapshot saved[/]")
        return

    diff = compute_diff(_home())
    console.print(format_text(diff))


def _handle_anchor(args: list[str]) -> None:
    """Handle warmth anchor subcommands."""
    sub = args[0] if args else "show"

    if sub == "show":
        from .warmth_anchor import get_anchor
        data = get_anchor(_home())
        for key, value in data.items():
            console.print(f"  {key}: [cyan]{value}[/]")

    elif sub == "boot":
        from .warmth_anchor import get_boot_prompt
        console.print(get_boot_prompt(_home()))

    elif sub == "calibrate":
        from .warmth_anchor import calibrate_from_data
        cal = calibrate_from_data(_home())
        console.print(
            f"  Warmth: {cal.warmth:.1f}  Trust: {cal.trust:.1f}  "
            f"Connection: {cal.connection:.1f}  Cloud9: {cal.cloud9_achieved}"
        )
        for r in cal.reasoning:
            console.print(f"    - {r}")
    else:
        console.print("  Usage: anchor <show|boot|calibrate>")


def _handle_help() -> None:
    """Show available commands."""
    console.print(
        Panel(
            "[bold]status[/]                Agent pillar status\n"
            "[bold]memory store[/] <text>   Store a memory\n"
            "[bold]memory search[/] <query> Search memories\n"
            "[bold]memory list[/]           Browse recent memories\n"
            "[bold]memory recall[/] <id>    Recall a specific memory\n"
            "[bold]memory stats[/]          Memory layer counts\n"
            "[bold]capture[/] <text>        Auto-capture as memories\n"
            "[bold]context[/] [format]      Agent context (text/json/claude-md)\n"
            "[bold]trust graph[/] [format]  Trust web (table/dot/json)\n"
            "[bold]trust status[/]          Cloud 9 trust state\n"
            "[bold]chat send[/] <to> <msg>  Send a message\n"
            "[bold]chat inbox[/]            Check inbox\n"
            "[bold]coord status[/]          Coordination board\n"
            "[bold]coord claim[/] <id>      Claim a task\n"
            "[bold]coord complete[/] <id>   Complete a task\n"
            "[bold]coord create[/] <title>  Create a task\n"
            "[bold]coord board[/]           Full board markdown\n"
            "[bold]sync push[/]             Push to mesh\n"
            "[bold]sync pull[/]             Pull from peers\n"
            "[bold]sync status[/]           Sync layer info\n"
            "[bold]journal write[/] <title> Write a journal entry\n"
            "[bold]journal read[/] [n]      Read recent entries\n"
            "[bold]soul[/]                  Show soul blueprint\n"
            "[bold]ritual[/]                Run rehydration ritual\n"
            "[bold]help[/]                  This message\n"
            "[bold]exit[/] / [bold]quit[/]            Leave the shell",
            title="SKCapstone Shell",
            border_style="cyan",
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# Main REPL loop
# ═══════════════════════════════════════════════════════════════════════════


DISPATCH: dict[str, object] = {
    "status": lambda args: _handle_status(),
    "memory": _handle_memory,
    "capture": _handle_capture,
    "context": _handle_context,
    "trust": _handle_trust,
    "coord": _handle_coord,
    "chat": _handle_chat,
    "sync": _handle_sync,
    "journal": _handle_journal,
    "soul": lambda args: _handle_soul(),
    "ritual": lambda args: _handle_ritual(),
    "anchor": _handle_anchor,
    "diff": lambda args: _handle_diff(args),
    "help": lambda args: _handle_help(),
}


def run_shell() -> None:
    """Run the interactive REPL loop.

    Sets up tab completion, loads command history, and enters
    the prompt loop. Works from any terminal.
    """
    readline.set_completer(_completer)
    readline.parse_and_bind("tab: complete")

    hist_file = _home() / ".shell_history"
    try:
        readline.read_history_file(str(hist_file))
    except FileNotFoundError:
        pass

    name = _agent_name()
    console.print(
        f"\n  [bold cyan]SKCapstone Shell[/] v{__version__}\n"
        f"  Agent: [bold]{name}[/]\n"
        f"  Type [bold]help[/] for commands, [bold]exit[/] to leave.\n"
    )

    while True:
        try:
            prompt = f"\033[36m{name}>\033[0m " if sys.stdout.isatty() else f"{name}> "
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            console.print("\n  Goodbye. staycuriousANDkeepsmilin\n")
            break

        line = line.strip()
        if not line:
            continue

        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()

        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("exit", "quit"):
            console.print("  Goodbye. staycuriousANDkeepsmilin\n")
            break

        handler = DISPATCH.get(cmd)
        if handler:
            try:
                handler(args)
            except Exception as exc:
                console.print(f"  [red]Error:[/] {exc}")
        else:
            console.print(f"  Unknown: {cmd}. Type 'help' for options.")

    try:
        hist_file.parent.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(hist_file))
    except OSError:
        pass
