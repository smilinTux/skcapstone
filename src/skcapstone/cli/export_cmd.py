"""Export and import commands for portable agent state bundles."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ._common import AGENT_HOME, console

from rich.panel import Panel


def register_export_commands(main: click.Group) -> None:
    """Register the export and import commands."""

    @main.command("export")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option(
        "--output", "-o", default=None, type=click.Path(),
        help="Output file path. Defaults to stdout.",
    )
    @click.option(
        "--pretty/--compact", default=True,
        help="Pretty-print JSON (default) or compact output.",
    )
    def export_cmd(home: str, output: str | None, pretty: bool):
        """Export the full agent state as a portable JSON bundle.

        Collects identity, soul overlays, memories, conversations, and
        config into a single JSON document. Use ``--output`` to write to
        a file, or pipe stdout for use in scripts.

        Examples:

            skcapstone export

            skcapstone export --output agent-snapshot.json

            skcapstone export --compact | gzip > snapshot.json.gz
        """
        from ..export import export_bundle

        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print(f"[red]Agent home not found: {home_path}[/]")
            raise SystemExit(1)

        try:
            bundle = export_bundle(home_path)
        except Exception as exc:
            console.print(f"[red]Export failed: {exc}[/]")
            raise SystemExit(1)

        indent = 2 if pretty else None
        serialized = json.dumps(bundle, indent=indent, ensure_ascii=False)

        if output:
            out_path = Path(output).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(serialized, encoding="utf-8")
            size_kb = out_path.stat().st_size / 1024
            console.print(Panel(
                f"[bold green]Export complete[/]\n"
                f"Agent: {bundle['agent_name']}\n"
                f"Memories: {len(bundle['memories'])}\n"
                f"Conversations: {len(bundle['conversations'])}\n"
                f"Soul overlays: {len((bundle.get('soul') or {}).get('installed') or {})}\n"
                f"Size: {size_kb:.1f} KB\n"
                f"File: [cyan]{out_path}[/]",
                title="Agent Bundle Exported",
                border_style="green",
            ))
        else:
            sys.stdout.write(serialized)
            sys.stdout.write("\n")
            sys.stdout.flush()

    @main.command("import")
    @click.argument("bundle_file", metavar="BUNDLE")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Target agent home directory.")
    @click.option("--overwrite-identity", is_flag=True, help="Overwrite existing identity file.")
    @click.option("--overwrite-config", is_flag=True, help="Overwrite existing config file.")
    @click.option("--overwrite-soul", is_flag=True, help="Overwrite existing soul files.")
    def import_cmd(
        bundle_file: str,
        home: str,
        overwrite_identity: bool,
        overwrite_config: bool,
        overwrite_soul: bool,
    ):
        """Import an agent state bundle into a home directory.

        Memories are always merged (existing memories with the same ID are
        kept). Conversations are merged per peer. Identity, config, and soul
        are only written when their target file is absent — unless the
        corresponding ``--overwrite-*`` flag is passed.

        Examples:

            skcapstone import agent-snapshot.json

            skcapstone import snapshot.json --home ~/.skcapstone-new

            skcapstone import snapshot.json --overwrite-identity --overwrite-config
        """
        from ..export import import_bundle

        bundle_path = Path(bundle_file).expanduser()
        if not bundle_path.exists():
            console.print(f"[red]Bundle file not found: {bundle_path}[/]")
            raise SystemExit(1)

        try:
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            console.print(f"[red]Cannot read bundle: {exc}[/]")
            raise SystemExit(1)

        home_path = Path(home).expanduser()

        try:
            result = import_bundle(
                home=home_path,
                bundle=bundle,
                overwrite_identity=overwrite_identity,
                overwrite_config=overwrite_config,
                overwrite_soul=overwrite_soul,
            )
        except ValueError as exc:
            console.print(f"[red]Invalid bundle: {exc}[/]")
            raise SystemExit(1)
        except Exception as exc:
            console.print(f"[red]Import failed: {exc}[/]")
            raise SystemExit(1)

        identity_status = "[green]written[/]" if result["identity_written"] else "[dim]skipped[/]"
        config_status = "[green]written[/]" if result["config_written"] else "[dim]skipped[/]"

        console.print(Panel(
            f"[bold green]Import complete[/]\n"
            f"Memories imported:      {result['memories_imported']}\n"
            f"Conversations imported: {result['conversations_imported']}\n"
            f"Soul files written:     {result['soul_files_written']}\n"
            f"Identity:               {identity_status}\n"
            f"Config:                 {config_status}",
            title="Agent Bundle Imported",
            border_style="green",
        ))

        if result["errors"]:
            console.print("[yellow]Warnings:[/]")
            for err in result["errors"]:
                console.print(f"  [dim]{err}[/]")
