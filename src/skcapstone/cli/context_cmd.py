"""Context loader commands: show, generate."""

from __future__ import annotations

from pathlib import Path

import click

from ._common import SKCAPSTONE_AGENT, console, resolve_agent_home
from ._validators import validate_file_path


def register_context_commands(main: click.Group) -> None:
    """Register the context command group."""

    @main.group()
    def context():
        """Universal AI agent context loader.

        Outputs agent identity, pillar status, board state, and recent
        memories in formats consumable by any AI tool. Tool-agnostic:
        works with Claude Code, Codex, OpenCode, Pi (AGENTS.md), Cursor,
        Windsurf, Aider, or any terminal.
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
        type=click.Choice(["text", "json", "claude-md", "agents-md", "cursor-rules"]),
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
            skcapstone context show --format agents-md     # AGENTS.md (any agent)
            skcapstone context show | claude               # pipe to Claude Code CLI

        Examples:
            skcapstone context show --format claude-md > CLAUDE.md
            skcapstone context show --format agents-md > AGENTS.md
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
        type=click.Choice(["claude-md", "agents-md", "cursor-rules", "both", "all"]),
        default="both",
        help=(
            "Which config file(s) to generate. both = claude-md + cursor-rules; "
            "all = AGENTS.md + a CLAUDE.md '@AGENTS.md' shim + cursor-rules."
        ),
    )
    def context_generate(home: str, memories: int, target: str):
        """Auto-generate AI tool config files from agent context.

        Writes CLAUDE.md (for Claude Code CLI), AGENTS.md (for Claude Code,
        Codex, OpenCode, Pi) and/or .cursor/rules/agent.mdc (for Cursor) in
        the current directory. With --target all, CLAUDE.md is a thin
        ``@AGENTS.md`` import shim rather than a second copy of the content.

        Examples:
            skcapstone context generate                   # CLAUDE.md + cursor rules
            skcapstone context generate --target claude-md # CLAUDE.md only
            skcapstone context generate --target agents-md # AGENTS.md only
            skcapstone context generate --target all       # AGENTS.md + shim + rules
        """
        from ..context_loader import FORMATTERS, gather_context

        home_path = Path(home).expanduser()
        ctx = gather_context(home_path, memory_limit=memories)

        cwd = Path.cwd()

        if target in ("agents-md", "all"):
            agents_path = cwd / "AGENTS.md"
            agents_path.write_text(FORMATTERS["agents-md"](ctx), encoding="utf-8")
            console.print(f"  [green]Written:[/] {agents_path}")

        if target in ("claude-md", "both"):
            claude_path = cwd / "CLAUDE.md"
            claude_path.write_text(FORMATTERS["claude-md"](ctx), encoding="utf-8")
            console.print(f"  [green]Written:[/] {claude_path}")
        elif target == "all":
            from ..claude_md import write_claude_md_shim

            claude_path = cwd / "CLAUDE.md"
            write_claude_md_shim(claude_path)
            console.print(f"  [green]Written:[/] {claude_path} (@AGENTS.md shim)")

        if target in ("cursor-rules", "both", "all"):
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
        "--target",
        type=click.Choice(["agents-md", "claude-md", "both"]),
        default="agents-md",
        help=(
            "Which context file to write (default: agents-md). both = full AGENTS.md "
            "plus a thin CLAUDE.md that imports it via '@AGENTS.md'."
        ),
    )
    @click.option(
        "--dest",
        default=None,
        type=click.Path(),
        help=(
            "Destination file or directory (default: repo root or cwd). With "
            "--target both, names the AGENTS.md file; the CLAUDE.md shim goes beside it."
        ),
    )
    @click.option(
        "--backup",
        is_flag=True,
        default=False,
        help="Rename each existing file to .bak before overwriting it.",
    )
    def refresh_context(home: str, memories: int, target: str, dest: str | None, backup: bool):
        """Regenerate AGENTS.md and/or CLAUDE.md from current agent state.

        Writes to the git repository root (or cwd if not in a git
        repository). AGENTS.md is read by Claude Code, Codex, OpenCode and
        Pi, so it is the default. Writing AGENTS.md never creates a
        CLAUDE.md; --target both writes CLAUDE.md as an ``@AGENTS.md``
        import shim instead of duplicating the content. Useful as a
        pre-commit hook or alias.

        Examples:
            skcapstone refresh-context                     # AGENTS.md
            skcapstone refresh-context --target claude-md  # CLAUDE.md
            skcapstone refresh-context --target both       # AGENTS.md + shim
            skcapstone refresh-context --dest /path/to/project/AGENTS.md
            skcapstone refresh-context --backup
        """
        import subprocess

        from ..claude_md import write_agents_md, write_claude_md, write_claude_md_shim

        home_path = Path(home).expanduser()
        filename = "CLAUDE.md" if target == "claude-md" else "AGENTS.md"

        if dest:
            validate_file_path(dest)
            out = Path(dest).expanduser().resolve()
            if out.is_dir():
                out = out / filename
        else:
            # Walk up from cwd to find git root; fall back to cwd.
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                out = Path(result.stdout.strip()) / filename
            except (subprocess.CalledProcessError, FileNotFoundError):
                out = Path.cwd() / filename

        if target == "claude-md":
            write_claude_md(home_path, out, memory_limit=memories, backup=backup)
            console.print(f"  [green]Written:[/] {out}")
            return

        shim = out.parent / "CLAUDE.md"
        if target == "both" and shim == out:
            raise click.BadParameter(
                "with --target both, --dest must name the AGENTS.md file, not CLAUDE.md",
                param_hint="--dest",
            )

        write_agents_md(home_path, out, memory_limit=memories, backup=backup)
        console.print(f"  [green]Written:[/] {out}")

        if target == "both":
            write_claude_md_shim(shim, out.name, backup=backup)
            console.print(f"  [green]Written:[/] {shim} (@{out.name} shim)")
