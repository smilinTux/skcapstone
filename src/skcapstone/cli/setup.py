"""Setup and lifecycle commands: init, install, uninstall, connect, onboard."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
import yaml

from ._common import AGENT_HOME, __version__, console, status_icon, consciousness_banner
from ._validators import validate_agent_name
from ..models import AgentConfig, PillarStatus, SyncConfig
from ..pillars.identity import generate_identity
from ..pillars.memory import initialize_memory
from ..pillars.security import audit_event, initialize_security
from ..pillars.sync import initialize_sync
from ..pillars.trust import initialize_trust
from ..runtime import get_runtime

from rich.panel import Panel


def _get_claude_template_dir() -> Path:
    """Return the bundled defaults/claude skeleton directory."""
    return Path(__file__).parent.parent / "defaults" / "claude"


def _write_global_claude_md(home_path: Path, agent_name: str) -> Optional[Path]:
    """Write ~/.claude/CLAUDE.md from the bundled skeleton template.

    The template lives at defaults/claude/CLAUDE.md inside the package.
    {{AGENT_NAME}} is substituted with the actual agent name.
    If the template is missing, falls back to a minimal generated file.
    """
    import platform

    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", "")
        claude_dir = Path(appdata) / ".claude" if appdata else Path.home() / "AppData" / "Roaming" / ".claude"
    else:
        claude_dir = Path.home() / ".claude"

    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        claude_md = claude_dir / "CLAUDE.md"

        template_path = _get_claude_template_dir() / "CLAUDE.md"
        if template_path.exists():
            content = template_path.read_text(encoding="utf-8")
            content = content.replace("{{AGENT_NAME}}", agent_name)
        else:
            # Minimal fallback if template is missing
            content = (
                f"# Claude Code — Global Agent Instructions ({agent_name})\n\n"
                f"- **Agent**: `{agent_name}`\n"
                f"- **Home**: `{home_path}`\n"
                f"- **Env**: `SKCAPSTONE_AGENT={agent_name}`\n\n"
                "Hooks auto-inject on SessionStart: soul + FEB chain + memories.\n\n"
                "> Regenerate with: `skcapstone context generate --target claude-md`\n"
            )

        claude_md.write_text(content, encoding="utf-8")
        return claude_md
    except OSError:
        return None


def _write_claude_settings(merge: bool = True) -> Optional[Path]:
    """Write (or merge) ~/.claude/settings.json with SK hook registrations.

    Uses the bundled defaults/claude/settings.json template, substituting
    {{SKMEMORY_HOOKS_DIR}} with the real skmemory hooks path.

    Args:
        merge: If True and settings.json already exists, merge hooks rather
               than overwrite. Default True.

    Returns:
        Path to the written settings.json, or None on failure.
    """
    import platform

    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", "")
        claude_dir = Path(appdata) / ".claude" if appdata else Path.home() / "AppData" / "Roaming" / ".claude"
    else:
        claude_dir = Path.home() / ".claude"

    try:
        import skmemory
        hooks_dir = str(Path(skmemory.__file__).parent / "hooks")
    except ImportError:
        return None  # skmemory not installed — caller should use skmemory register instead

    template_path = _get_claude_template_dir() / "settings.json"
    if not template_path.exists():
        return None

    raw = template_path.read_text(encoding="utf-8")
    raw = raw.replace("{{SKMEMORY_HOOKS_DIR}}", hooks_dir)
    new_settings = json.loads(raw)

    settings_path = claude_dir / "settings.json"
    if merge and settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

        # Merge hooks: add new hooks that aren't already registered
        existing_hooks = existing.get("hooks", {})
        for event, hook_groups in new_settings.get("hooks", {}).items():
            existing_event = existing_hooks.setdefault(event, [])
            existing_commands = {
                h.get("command")
                for group in existing_event
                for h in group.get("hooks", [])
                if "command" in h
            }
            for group in hook_groups:
                cmds = {h.get("command") for h in group.get("hooks", []) if "command" in h}
                if not cmds.issubset(existing_commands):
                    existing_event.append(group)
        existing["hooks"] = existing_hooks
        # Preserve non-hook keys from template (skipDangerousModePermissionPrompt, etc.)
        for k, v in new_settings.items():
            if k != "hooks":
                existing.setdefault(k, v)
        final = existing
    else:
        claude_dir.mkdir(parents=True, exist_ok=True)
        final = new_settings

    settings_path.write_text(json.dumps(final, indent=2), encoding="utf-8")
    return settings_path


def register_setup_commands(main: click.Group) -> None:
    """Register all setup/lifecycle commands on the main CLI group."""

    @main.command()
    @click.option(
        "--home",
        default=AGENT_HOME,
        help="Agent home directory.",
        type=click.Path(),
    )
    def init(home: str):
        """Initialize a sovereign agent (interactive wizard).

        Alias for 'skcapstone onboard' — runs the full 13-step setup wizard.
        Creates ~/.skcapstone/ with identity, memory, trust, security, soul,
        and connects to the mesh. Zero to sovereign in under 5 minutes.
        """
        from ..onboard import run_onboard

        run_onboard(home)

    @main.command("install")
    @click.option("--name", default=None, help="Name for your sovereign agent.")
    @click.option("--email", default=None, help="Email for the agent identity.")
    @click.option("--home", default=AGENT_HOME, help="Agent home directory.", type=click.Path())
    @click.option("--skip-deps", is_flag=True, help="Skip installing ecosystem packages.")
    @click.option("--skip-seeds", is_flag=True, help="Skip importing Cloud 9 seeds.")
    @click.option("--skip-ritual", is_flag=True, help="Skip the rehydration ritual.")
    @click.option("--skip-preflight", is_flag=True, help="Skip Git preflight check.")
    @click.option("--path", "install_path", default=None, type=click.IntRange(1, 3),
                  help="Pre-select install path: 1=fresh, 2=join, 3=update.")
    def install_cmd(name, email, home, skip_deps, skip_seeds, skip_ritual, skip_preflight, install_path):
        """Guided setup wizard — set up, join, or update your sovereign node."""
        from ..install_wizard import run_install_wizard

        run_install_wizard(
            name=name, email=email, home=home,
            skip_deps=skip_deps, skip_seeds=skip_seeds,
            skip_ritual=skip_ritual, skip_preflight=skip_preflight,
            path=install_path,
        )

    @main.command("uninstall")
    @click.option("--home", default=AGENT_HOME, help="Agent home directory.", type=click.Path())
    @click.option("--force", is_flag=True, help="Skip confirmations (for scripting).")
    @click.option("--keep-data", is_flag=True, help="Deregister only — keep local files.")
    @click.option(
        "--export-first",
        is_flag=True,
        help="Create a full backup archive before removing data.",
    )
    def uninstall_cmd(home, force, keep_data, export_first):
        """Remove this sovereign node completely."""
        from ..uninstall_wizard import run_uninstall_wizard

        run_uninstall_wizard(home=home, force=force, keep_data=keep_data, export_first=export_first)

    @main.command("install-gui")
    def install_gui_cmd():
        """Launch the graphical setup wizard (Windows-friendly)."""
        from ..gui_installer import main as gui_main

        gui_main()

    @main.command()
    @click.argument("platform")
    @click.option("--home", default=AGENT_HOME, help="Agent home directory.", type=click.Path())
    def connect(platform: str, home: str):
        """Connect a platform to the sovereign agent.

        Supported platforms: cursor, terminal, vscode, neovim, web
        """
        home_path = Path(home).expanduser()

        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run [bold]skcapstone init[/] first.")
            sys.exit(1)

        runtime = get_runtime(home_path)
        connector = runtime.register_connector(name=f"{platform} connector", platform=platform)
        audit_event(home_path, "CONNECT", f"Platform '{platform}' connected")

        console.print()
        console.print(
            f"[bold green]Connected:[/] {platform} "
            f"[dim]({connector.connected_at.isoformat() if connector.connected_at else 'now'})[/]"
        )
        console.print(
            f"[dim]Your agent '{runtime.manifest.name}' is now accessible from {platform}.[/]"
        )
        console.print()

    @main.command("onboard")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def onboard_cmd(home: str):
        """Interactive onboarding wizard for new humans and AI agents.

        \b
        Eight guided steps — zero to sovereign in under 5 minutes:
          1. Identity   — generate PGP keypair via CapAuth
          2. Soul       — create name, values, and personality blueprint
          3. Memory     — initialize SKMemory and import Cloud 9 seeds
          4. Ritual     — run the full rehydration ritual
          5. Trust      — verify trust chain from FEB files
          6. Mesh       — check Syncthing peering
          7. Heartbeat  — publish your first alive beacon
          8. Board      — register on the coordination board
        """
        from ..onboard import run_onboard

        run_onboard(home)

    @main.command("reset")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--force", is_flag=True, help="Skip confirmation prompt (for scripting/testing).")
    def reset_cmd(home: str, force: bool):
        """Factory reset — wipe all agent data.

        Backs up the identity/ directory to ~/.skcapstone-backup-{timestamp}/
        before deleting. All other data is permanently removed.
        """
        home_path = Path(home).expanduser()

        if not home_path.exists():
            console.print(f"[yellow]No agent home found at {home_path}. Nothing to reset.[/]")
            return

        if not force:
            console.print(
                f"\n[bold red]WARNING:[/] This will permanently delete all agent data at:\n"
                f"  [dim]{home_path}[/]\n"
            )
            answer = click.prompt(
                "Are you sure? This will delete all agent data. Type YES to confirm",
                default="",
                show_default=False,
            )
            if answer.strip() != "YES":
                console.print("[yellow]Reset aborted.[/]")
                return

        # Backup identity/ first
        identity_dir = home_path / "identity"
        backup_path: Path | None = None
        if identity_dir.exists():
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = home_path.parent / f".skcapstone-backup-{ts}"
            backup_path.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(identity_dir), str(backup_path / "identity"))
            console.print(f"  [dim]Identity backed up → {backup_path}[/]")

        # Wipe the home directory
        shutil.rmtree(str(home_path))
        console.print(f"[bold green]Reset complete.[/] All agent data deleted from {home_path}.")
        if backup_path:
            console.print(f"  [dim]Identity backup: {backup_path}[/]")
        console.print("[dim]Run 'skcapstone init' to start fresh.[/]")

    @main.command("shell")
    def shell_cmd():
        """Interactive REPL for sovereign agent operations."""
        from ..shell import run_shell

        run_shell()
