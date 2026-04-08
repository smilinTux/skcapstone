"""Context loader commands: show, generate."""

from __future__ import annotations

from pathlib import Path

import click

from ._common import AGENT_HOME, SKCAPSTONE_AGENT, console, resolve_agent_home


def register_context_commands(main: click.Group) -> None:
    """Register the context command group."""

    @main.group()
    def context():
        """Universal AI agent context loader.

        Outputs agent identity, pillar status, board state, and recent
        memories in formats consumable by any AI tool. Tool-agnostic:
        works with Claude Code, Cursor, Windsurf, Aider, or any terminal.
        """

    @context.command("show")
    @click.option(
        "--home",
        default=str(resolve_agent_home(SKCAPSTONE_AGENT)),
        type=click.Path(),
    )
    @click.option(
        "--format",
        "fmt",
        type=click.Choice(["text", "json", "claude-md", "cursor-rules"]),
        default="text",
        help="Output format (default: text).",
    )
    @click.option("--memories", "-n", default=10, help="Max recent memories to include.")
    def context_show(home: str, fmt: str, memories: int):
        """Show the agent's full context.

        Pipe into any AI tool or redirect to a file:

            skcapstone context show                        # terminal
            skcapstone context show --format json          # machine-readable
            skcapstone context show --format claude-md     # for Claude Code
            skcapstone context show | claude               # pipe to Claude Code CLI

        Examples:
            skcapstone context show --format claude-md > CLAUDE.md
            skcapstone context show --format cursor-rules > .cursor/rules/agent.mdc
        """
        home_path = Path(home).expanduser()
        if fmt == "claude-md":
            from ..claude_md import generate_claude_md
            click.echo(generate_claude_md(home_path, memory_limit=memories))
        else:
            from ..context_loader import FORMATTERS, gather_context
            ctx = gather_context(home_path, memory_limit=memories)
            click.echo(FORMATTERS[fmt](ctx))

    @context.command("generate")
    @click.option(
        "--home",
        default=str(resolve_agent_home(SKCAPSTONE_AGENT)),
        type=click.Path(),
    )
    @click.option("--memories", "-n", default=10, help="Max recent memories to include.")
    @click.option(
        "--target",
        type=click.Choice(["claude-md", "cursor-rules", "both"]),
        default="both",
        help="Which config file(s) to generate.",
    )
    def context_generate(home: str, memories: int, target: str):
        """Auto-generate AI tool config files from agent context.

        Writes CLAUDE.md (for Claude Code CLI) and/or
        .cursor/rules/agent.mdc (for Cursor) in the current directory.

        Examples:
            skcapstone context generate                   # both files
            skcapstone context generate --target claude-md # CLAUDE.md only
        """
        from ..context_loader import FORMATTERS, gather_context

        home_path = Path(home).expanduser()
        ctx = gather_context(home_path, memory_limit=memories)

        cwd = Path.cwd()

        if target in ("claude-md", "both"):
            claude_path = cwd / "CLAUDE.md"
            claude_path.write_text(FORMATTERS["claude-md"](ctx), encoding="utf-8")
            console.print(f"  [green]Written:[/] {claude_path}")

        if target in ("cursor-rules", "both"):
            rules_dir = cwd / ".cursor" / "rules"
            rules_dir.mkdir(parents=True, exist_ok=True)
            rules_path = rules_dir / "agent.mdc"
            rules_path.write_text(FORMATTERS["cursor-rules"](ctx), encoding="utf-8")
            console.print(f"  [green]Written:[/] {rules_path}")

        console.print()

    @main.command("refresh-context")
    @click.option(
        "--home",
        default=str(resolve_agent_home(SKCAPSTONE_AGENT)),
        type=click.Path(),
    )
    @click.option("--memories", "-n", default=10, help="Max recent memories to embed.")
    @click.option(
        "--dest",
        default=None,
        type=click.Path(),
        help="Destination path for CLAUDE.md (default: repo root or cwd).",
    )
    @click.option("--backup", is_flag=True, default=False, help="Rename existing CLAUDE.md to .bak before writing.")
    def refresh_context(home: str, memories: int, dest: str | None, backup: bool):
        """Regenerate CLAUDE.md from current agent state.

        Writes CLAUDE.md to the git repository root (or cwd if not in a
        git repository). Useful as a pre-commit hook or alias.

        Examples:
            skcapstone refresh-context
            skcapstone refresh-context --dest /path/to/project/CLAUDE.md
            skcapstone refresh-context --backup
        """
        import subprocess

        from ..claude_md import write_claude_md

        home_path = Path(home).expanduser()

        if dest:
            target = Path(dest).expanduser().resolve()
            if target.is_dir():
                target = target / "CLAUDE.md"
        else:
            # Walk up from cwd to find git root; fall back to cwd.
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                target = Path(result.stdout.strip()) / "CLAUDE.md"
            except (subprocess.CalledProcessError, FileNotFoundError):
                target = Path.cwd() / "CLAUDE.md"

        write_claude_md(home_path, target, memory_limit=memories, backup=backup)
        console.print(f"  [green]Written:[/] {target}")
