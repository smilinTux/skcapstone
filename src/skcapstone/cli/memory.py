"""Memory commands: store, search, list, recall, delete, stats, gc, curate, migrate, verify, reindex."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console, status_icon
from ..pillars.security import audit_event

from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def register_memory_commands(main: click.Group) -> None:
    """Register the memory command group."""

    @main.group()
    def memory():
        """Sovereign memory — your agent never forgets.

        Store, search, recall, and manage memories across
        sessions and platforms.
        """

    @memory.command("store")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.argument("content")
    @click.option("--tag", "-t", multiple=True, help="Tags for categorization.")
    @click.option("--source", "-s", default="cli", help="Memory source.")
    @click.option("--importance", "-i", default=0.5, type=float, help="Importance 0.0-1.0.")
    @click.option("--layer", "-l", type=click.Choice(["short-term", "mid-term", "long-term"]), default=None)
    def memory_store(home, content, tag, source, importance, layer):
        """Store a new memory."""
        from ..memory_engine import store as mem_store
        from ..models import MemoryLayer

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        lyr = MemoryLayer(layer) if layer else None
        entry = mem_store(home=home_path, content=content, tags=list(tag),
                          source=source, importance=importance, layer=lyr)

        console.print(f"\n  [green]Stored:[/] {entry.memory_id}")
        console.print(f"  Layer: [cyan]{entry.layer.value}[/]")
        console.print(f"  Tags: {', '.join(entry.tags) if entry.tags else '[dim]none[/]'}")
        console.print(f"  Importance: {entry.importance}")
        audit_event(home_path, "MEMORY_STORE", f"Memory {entry.memory_id} stored in {entry.layer.value}")
        console.print()

    @memory.command("search")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.argument("query")
    @click.option("--tag", "-t", multiple=True, help="Filter by tag.")
    @click.option("--layer", "-l", type=click.Choice(["short-term", "mid-term", "long-term"]), default=None)
    @click.option("--limit", "-n", default=20, help="Max results.")
    def memory_search(home, query, tag, layer, limit):
        """Search memories by content and tags."""
        from ..memory_engine import search as mem_search
        from ..models import MemoryLayer

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        lyr = MemoryLayer(layer) if layer else None
        tags = list(tag) if tag else None
        results = mem_search(home=home_path, query=query, layer=lyr, tags=tags, limit=limit)

        if not results:
            console.print(f"\n  [dim]No memories match '[/]{query}[dim]'[/]\n")
            return

        console.print(f"\n  [bold]{len(results)}[/] memor{'y' if len(results) == 1 else 'ies'} found:\n")

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style="cyan", max_width=14)
        table.add_column("Layer", style="dim")
        table.add_column("Content", max_width=50)
        table.add_column("Tags", style="dim")
        table.add_column("Imp", justify="right")

        for entry in results:
            preview = entry.content[:80] + ("..." if len(entry.content) > 80 else "")
            table.add_row(entry.memory_id, entry.layer.value, preview,
                          ", ".join(entry.tags) if entry.tags else "", f"{entry.importance:.1f}")

        console.print(table)
        console.print()

    @memory.command("list")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--layer", "-l", type=click.Choice(["short-term", "mid-term", "long-term"]), default=None)
    @click.option("--tag", "-t", multiple=True, help="Filter by tag.")
    @click.option("--limit", "-n", default=50, help="Max results.")
    def memory_list(home, layer, tag, limit):
        """Browse memories, newest first."""
        from ..memory_engine import list_memories as mem_list
        from ..models import MemoryLayer

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        lyr = MemoryLayer(layer) if layer else None
        tags = list(tag) if tag else None
        entries = mem_list(home=home_path, layer=lyr, tags=tags, limit=limit)

        if not entries:
            console.print("\n  [dim]No memories found.[/]\n")
            return

        console.print(f"\n  [bold]{len(entries)}[/] memor{'y' if len(entries) == 1 else 'ies'}:\n")

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style="cyan", max_width=14)
        table.add_column("Layer")
        table.add_column("Content", max_width=50)
        table.add_column("Tags", style="dim")
        table.add_column("Imp", justify="right")
        table.add_column("Accessed", justify="right", style="dim")

        for entry in entries:
            preview = entry.content[:80] + ("..." if len(entry.content) > 80 else "")
            layer_color = {"long-term": "green", "mid-term": "cyan", "short-term": "dim"}.get(entry.layer.value, "dim")
            table.add_row(entry.memory_id, Text(entry.layer.value, style=layer_color), preview,
                          ", ".join(entry.tags) if entry.tags else "", f"{entry.importance:.1f}",
                          str(entry.access_count))

        console.print(table)
        console.print()

    @memory.command("recall")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.argument("memory_id")
    def memory_recall(home, memory_id):
        """Recall a specific memory by ID."""
        from ..memory_engine import recall as mem_recall

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        entry = mem_recall(home=home_path, memory_id=memory_id)
        if entry is None:
            console.print(f"[red]Memory not found:[/] {memory_id}")
            sys.exit(1)

        console.print()
        console.print(Panel(
            entry.content,
            title=f"[cyan]{entry.memory_id}[/] — {entry.layer.value}",
            subtitle=f"importance={entry.importance} accessed={entry.access_count} source={entry.source}",
            border_style="bright_blue",
        ))
        if entry.tags:
            console.print(f"  Tags: {', '.join(entry.tags)}")
        if entry.metadata:
            console.print(f"  Metadata: {json.dumps(entry.metadata)}")
        console.print(f"  Created: {entry.created_at.isoformat() if entry.created_at else 'unknown'}")
        if entry.accessed_at:
            console.print(f"  Last accessed: {entry.accessed_at.isoformat()}")
        console.print()

    @memory.command("delete")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.argument("memory_id")
    @click.option("--force", is_flag=True, help="Skip confirmation.")
    def memory_delete(home, memory_id, force):
        """Delete a memory by ID."""
        from ..memory_engine import delete as mem_delete

        home_path = Path(home).expanduser()
        if not force and not click.confirm(f"Delete memory {memory_id}?"):
            console.print("[yellow]Aborted.[/]")
            return

        if mem_delete(home_path, memory_id):
            console.print(f"\n  [red]Deleted:[/] {memory_id}\n")
            audit_event(home_path, "MEMORY_DELETE", f"Memory {memory_id} deleted")
        else:
            console.print(f"[red]Memory not found:[/] {memory_id}")
            sys.exit(1)

    @memory.command("stats")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def memory_stats(home):
        """Show memory statistics across all layers."""
        from ..memory_engine import get_stats

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        stats = get_stats(home_path)
        console.print()
        console.print(Panel(
            f"Total: [bold]{stats.total_memories}[/] memories\n"
            f"  [green]Long-term:[/]  {stats.long_term}\n"
            f"  [cyan]Mid-term:[/]   {stats.mid_term}\n"
            f"  [dim]Short-term:[/] {stats.short_term}\n\n"
            f"Store: {stats.store_path}\n"
            f"Status: {status_icon(stats.status)}",
            title="SKMemory", border_style="bright_blue",
        ))
        console.print()

    @memory.command("gc")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def memory_gc(home):
        """Garbage-collect expired short-term memories."""
        from ..memory_engine import gc_expired

        home_path = Path(home).expanduser()
        removed = gc_expired(home_path)
        if removed:
            console.print(f"\n  [yellow]Cleaned up {removed} expired memor{'y' if removed == 1 else 'ies'}.[/]\n")
        else:
            console.print("\n  [green]Nothing to clean up.[/]\n")

    @memory.command("curate")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--dry-run", is_flag=True, help="Preview changes without applying.")
    @click.option("--promote", is_flag=True, help="Only run promotion pass.")
    @click.option("--dedupe", is_flag=True, help="Only run deduplication pass.")
    @click.option("--stats", is_flag=True, help="Show curation statistics only.")
    def memory_curate(home, dry_run, promote, dedupe, stats):
        """Curate memories: auto-tag, promote, deduplicate."""
        from ..memory_curator import MemoryCurator

        home_path = Path(home).expanduser()
        curator = MemoryCurator(home_path)

        if stats:
            s = curator.get_stats()
            console.print(f"\n  [bold]{s['total']}[/] memories")
            for lyr, count in s.get("layers", {}).items():
                console.print(f"    {lyr}: {count}")
            console.print(f"  Tag coverage: [bold]{s['tag_coverage']:.0%}[/]")
            console.print(f"  Avg importance: [bold]{s['avg_importance']:.2f}[/]")
            console.print(f"  Promotion candidates: [bold]{s['promotion_candidates']}[/]")
            if s.get("top_tags"):
                console.print("  Top tags:")
                for tg, count in s["top_tags"][:10]:
                    console.print(f"    {tg}: {count}")
            console.print()
            return

        run_promote = promote or (not promote and not dedupe)
        run_dedupe = dedupe or (not promote and not dedupe)

        prefix = "[DRY RUN] " if dry_run else ""
        console.print(f"\n  {prefix}Running curation pass...\n")

        result = curator.curate(dry_run=dry_run, promote=run_promote, dedupe=run_dedupe)

        console.print(f"  Scanned: {result.total_scanned} memories")
        if result.tagged:
            console.print(f"  [cyan]Tagged:[/] {len(result.tagged)} memories received new tags")
        if result.promoted:
            console.print(f"  [green]Promoted:[/] {len(result.promoted)} memories moved to higher tier")
        if result.deduped:
            console.print(f"  [yellow]Deduped:[/] {len(result.deduped)} duplicate(s) removed")
        if not result.tagged and not result.promoted and not result.deduped:
            console.print("  [dim]Nothing to curate — memories are clean.[/]")
        console.print()

    @memory.command("migrate")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--dry-run", is_flag=True, help="Preview without writing.")
    @click.option("--verify", is_flag=True, help="Verify migration integrity.")
    def memory_migrate(home, dry_run, verify):
        """Migrate JSON memories to the unified three-tier backend."""
        from ..migrate_memories import migrate

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        result = migrate(home_path, dry_run=dry_run, verify=verify)

        if dry_run:
            console.print(f"\n  [bold]DRY RUN:[/] Found {result['total_json']} JSON memories to migrate.\n")
            return

        if verify:
            verified = result.get("verified", 0)
            missing = result.get("missing", [])
            if not missing:
                console.print(f"\n  [green]Verified:[/] All {verified} memories present in unified backend.\n")
            else:
                console.print(f"\n  [yellow]Verification:[/] {verified} present, {len(missing)} missing.")
                for mid in missing[:10]:
                    console.print(f"    [red]Missing:[/] {mid}")
                if len(missing) > 10:
                    console.print(f"    ... and {len(missing) - 10} more")
                console.print()
            return

        console.print(f"\n  [bold]Migration results:[/]")
        console.print(f"    Total JSON memories: {result['total_json']}")
        console.print(f"    [green]Migrated:[/] {result['migrated']}")
        console.print(f"    [dim]Skipped (existing):[/] {result['skipped_existing']}")
        if result.get("errors"):
            console.print(f"    [red]Errors:[/] {len(result['errors'])}")
            for err in result["errors"][:5]:
                console.print(f"      {err}")
        console.print()

    @memory.command("verify")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def memory_verify(home):
        """Check consistency across memory backends."""
        from ..memory_adapter import verify_sync

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        result = verify_sync()

        console.print("\n  [bold]Backend sync status:[/]")
        for name, info in result.get("backends", {}).items():
            ok = info.get("ok", False)
            count = info.get("count", "?")
            icon = "[green]ok[/]" if ok else "[red]error[/]"
            console.print(f"    {name}: {icon} ({count} memories)")

        if result.get("synced"):
            console.print("\n  [green]All backends in sync.[/]\n")
        else:
            console.print(f"\n  [yellow]Out of sync:[/] {result.get('reason', 'unknown')}\n")

    @memory.command("reindex")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def memory_reindex(home):
        """Rebuild vector and graph indexes from SQLite primary."""
        from ..memory_adapter import reindex_all

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        console.print("\n  Reindexing secondary backends...\n")
        result = reindex_all()

        if result.get("ok"):
            console.print(f"  [green]Done:[/] {result['total']} memories reindexed.")
            console.print(f"    Vector: {result['vector_indexed']}")
            console.print(f"    Graph:  {result['graph_indexed']}")
        else:
            console.print(f"  [red]Errors during reindex.[/]")
            for err in result.get("errors", [])[:5]:
                console.print(f"    {err}")
        console.print()
