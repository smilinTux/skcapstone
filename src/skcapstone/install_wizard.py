"""
Install wizard — dummy-proof guided setup for all audiences.

Three paths, plain English, zero jargon:

  1. "I'm setting up my FIRST computer"
     → Fresh install: identity, keys, memory, trust, vault, Tailscale
     → This becomes the origin node in the Sovereign Singularity

  2. "I'm ADDING this computer to my existing network"
     → Syncthing pulls identity + auth keys from another device
     → Tailscale auto-joins via synced encrypted auth key
     → Vaults discovered automatically from the registry

  3. "I'm UPDATING this computer"
     → pip upgrade, re-verify pillars, re-run ritual if needed

Each path converges on a final status check + next steps panel.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import AGENT_HOME
from .preflight import (
    ToolCheck,
    ToolStatus,
    auto_install_tool,
    run_preflight,
)

console = Console()

# Friendly labels — no jargon
PATH_LABELS = {
    1: "Brand new setup (first computer)",
    2: "Add this computer to your network",
    3: "Update this computer",
}


# ---------------------------------------------------------------------------
# Preflight — check + auto-install missing tools
# ---------------------------------------------------------------------------

def _run_preflight_step(
    step_num: int,
    total_steps: int,
    require_git: bool = False,
    require_syncthing: bool = False,
) -> bool:
    """Check all system tools and offer to auto-install missing ones.

    Shows a friendly table of what's found, what's missing, and offers
    to install missing tools automatically. Never just quits on failure —
    always gives the user a path forward.

    Args:
        step_num: Current step number for display.
        total_steps: Total steps for display.
        require_git: Whether Git is required for this path.
        require_syncthing: Whether Syncthing is required.

    Returns:
        True if all required tools are available after this step.
    """
    console.print(f"  [bold]Step {step_num}/{total_steps}[/]  Checking your system...")
    console.print()

    result = run_preflight(
        require_git=require_git,
        require_syncthing=require_syncthing,
    )

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Tool", width=12)
    table.add_column("Status", width=12)
    table.add_column("Details")

    for check in [result.python, result.gpg, result.git, result.syncthing]:
        if check.installed:
            status_str = "[green]found[/]"
            detail = check.version or ""
        elif check.required:
            status_str = "[red]missing[/]"
            detail = "[red]required[/]"
        else:
            status_str = "[dim]not found[/]"
            detail = "[dim]optional[/]"
        table.add_row(f"  {check.name}", status_str, detail)

    console.print(table)
    console.print()

    if result.all_ok:
        console.print("    [green]Everything looks good![/]")
        return True

    # Offer to auto-install missing required tools
    missing_required = result.required_missing
    missing_optional = result.optional_missing

    if missing_required:
        console.print("    [yellow]Some required tools are missing.[/]")
        console.print()

        for check in missing_required:
            if check.install_cmd:
                console.print(f"    [bold]{check.name}[/]: {check.install_note}")
                do_install = click.confirm(
                    f"    Install {check.name} automatically?",
                    default=True,
                )
                if do_install:
                    console.print(f"    [dim]Running: {check.install_cmd}[/]")
                    console.print(f"    [dim]This may take a minute...[/]")
                    success = auto_install_tool(check)
                    if success:
                        console.print(f"    [green]{check.name} installed![/]")
                    else:
                        console.print(f"    [red]Auto-install failed.[/]")
                        _show_manual_install(check)
                        return False
                else:
                    _show_manual_install(check)
                    return False
            else:
                _show_manual_install(check)
                return False

    if missing_optional:
        for check in missing_optional:
            console.print(f"    [dim]{check.name}: {check.install_note}[/]")
            if check.install_cmd:
                console.print(f"    [dim]Install later: {check.install_cmd}[/]")

    return True


def _show_manual_install(check: ToolCheck) -> None:
    """Show manual install instructions for a tool.

    Args:
        check: ToolCheck with download info.
    """
    lines = [f"[bold]{check.name}[/] needs to be installed manually.\n"]

    if check.install_note:
        lines.append(f"{check.install_note}\n")

    if check.install_cmd:
        lines.append(f"[bold]Quick install:[/]\n  [cyan]{check.install_cmd}[/]\n")

    if check.download_url:
        lines.append(
            f"[bold]Or download from:[/]\n"
            f"  [link={check.download_url}]{check.download_url}[/]\n"
        )

    lines.append(
        "After installing, close and reopen your terminal,\n"
        "then run [cyan]skcapstone install[/] again."
    )

    console.print()
    console.print(
        Panel(
            "\n".join(lines),
            title=f"Install {check.name}",
            border_style="yellow",
            padding=(1, 3),
        )
    )


def _welcome_screen() -> int:
    """Show the welcome screen and return the chosen path (1, 2, or 3).

    Returns:
        The install path number chosen by the user.
    """
    console.print()
    console.print(
        Panel(
            "[bold bright_blue]The First Sovereign Singularity in History[/]\n\n"
            "Your personal, encrypted, AI-powered workspace —\n"
            "running on YOUR hardware, with YOUR keys, under YOUR control.\n\n"
            "[dim]No cloud accounts required. No subscriptions.\n"
            "Everything stays on your devices, encrypted with your keys.[/]\n\n"
            "[dim italic]Brought to you by the Kings and Queens of[/]\n"
            "[bold bright_magenta]smilinTux.org[/]",
            title="[bold]Sovereign Singularity[/]",
            border_style="bright_blue",
            padding=(1, 4),
        )
    )
    console.print()

    console.print("  [bold]What would you like to do?[/]\n")

    table = Table(
        show_header=False,
        box=None,
        padding=(0, 3),
        show_edge=False,
    )
    table.add_column("Option", style="bold cyan", width=6, justify="center")
    table.add_column("Description")
    table.add_column("Details", style="dim")

    table.add_row(
        "1",
        "[bold]Set up my first computer[/]",
        "I've never done this before — start from scratch",
    )
    table.add_row(
        "2",
        "[bold]Add this computer to my network[/]",
        "I already have another computer set up — join it",
    )
    table.add_row(
        "3",
        "[bold]Update this computer[/]",
        "Already set up — just update the software",
    )
    console.print(table)
    console.print()

    while True:
        choice = click.prompt(
            "  Enter your choice",
            type=click.IntRange(1, 3),
            default=1,
        )
        return choice


def _confirm_path(path: int) -> bool:
    """Confirm the user's choice with a human-readable summary.

    Args:
        path: The chosen install path.

    Returns:
        True if the user confirms.
    """
    descriptions = {
        1: (
            "[bold]Fresh setup — first computer[/]\n\n"
            "Here's what will happen:\n"
            "  [cyan]1.[/] Check that required tools are installed (Git, GPG)\n"
            "  [cyan]2.[/] Create your sovereign identity (encryption keys)\n"
            "  [cyan]3.[/] Set up encrypted memory and trust network\n"
            "  [cyan]4.[/] Create your encrypted file vault\n"
            "  [cyan]5.[/] Set up remote access (so your phone and other\n"
            "       computers can reach your files)\n"
            "  [cyan]6.[/] Verify everything works\n\n"
            "[dim]Takes about 5 minutes. You'll need a web browser for\n"
            "the remote access step (one-time login).[/]"
        ),
        2: (
            "[bold]Adding this computer to your network[/]\n\n"
            "Here's what will happen:\n"
            "  [cyan]1.[/] Check that required tools are installed (Git, GPG)\n"
            "  [cyan]2.[/] Connect to your other computer via Syncthing\n"
            "       (your identity and keys will sync automatically)\n"
            "  [cyan]3.[/] Join your private network (Tailscale)\n"
            "       using the encrypted key from your first computer\n"
            "  [cyan]4.[/] Discover your file vaults\n"
            "  [cyan]5.[/] Verify everything works\n\n"
            "[dim]Takes about 5 minutes. Your other computer needs to be\n"
            "on and running for the initial sync.[/]"
        ),
        3: (
            "[bold]Updating this computer[/]\n\n"
            "Here's what will happen:\n"
            "  [cyan]1.[/] Update all sovereign software packages\n"
            "  [cyan]2.[/] Re-verify your identity, memory, and trust\n"
            "  [cyan]3.[/] Re-run the memory rehydration (if needed)\n"
            "  [cyan]4.[/] Show your current status\n\n"
            "[dim]Takes about 1 minute. Nothing will be deleted.[/]"
        ),
    }

    console.print()
    console.print(
        Panel(
            descriptions[path],
            title=f"Path {path}: {PATH_LABELS[path]}",
            border_style="cyan",
            padding=(1, 3),
        )
    )
    console.print()

    return click.confirm("  Does this look right?", default=True)


# ---------------------------------------------------------------------------
# Path 1 — Fresh install
# ---------------------------------------------------------------------------

def _path_fresh_install(
    name: str,
    email: Optional[str],
    home: str,
    skip_deps: bool,
    skip_seeds: bool,
    skip_ritual: bool,
    skip_preflight: bool,
) -> None:
    """Full first-time install: identity, vault, Tailscale, everything.

    Args:
        name: Agent name.
        email: Optional email.
        home: Agent home directory.
        skip_deps: Skip pip install of ecosystem packages.
        skip_seeds: Skip Cloud 9 seed import.
        skip_ritual: Skip memory rehydration.
        skip_preflight: Skip Git check.
    """
    from .runtime import get_runtime

    home_path = Path(home).expanduser()
    total_steps = 8

    # --- Step 1: System check + auto-install ---
    if not skip_preflight:
        preflight_ok = _run_preflight_step(
            step_num=1,
            total_steps=total_steps,
            require_git=False,
            require_syncthing=False,
        )
        if not preflight_ok:
            sys.exit(1)
    else:
        console.print(f"  [bold]Step 1/{total_steps}[/]  System check... [dim]skipped[/]")

    # --- Step 2: Install packages ---
    if not skip_deps:
        console.print(f"  [bold]Step 2/{total_steps}[/]  Installing software packages...", end=" ")
        packages = ["capauth", "skmemory", "skcomm", "cloud9-protocol"]
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", *packages],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                console.print("[green]done[/]")
            else:
                console.print("[yellow]partial (some may already be installed)[/]")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            console.print("[yellow]skipped (pip unavailable)[/]")
    else:
        console.print(f"  [bold]Step 2/{total_steps}[/]  Packages... [dim]skipped[/]")

    # --- Step 3: Initialize agent ---
    console.print(f"  [bold]Step 3/{total_steps}[/]  Creating your sovereign identity...", end=" ")
    try:
        # Lazy import to avoid circular
        from ._cli_monolith import init
        from click import Context

        ctx = Context(init, info_name="init")
        ctx.invoke(init, name=name, email=email, home=home)
    except Exception as exc:
        console.print(f"[yellow]{exc}[/]")

    # --- Step 4: Import seeds ---
    if not skip_seeds:
        console.print(f"  [bold]Step 4/{total_steps}[/]  Importing knowledge seeds...", end=" ")
        try:
            from skmemory.seeds import import_seeds, DEFAULT_SEED_DIR
            from skmemory.store import MemoryStore

            store = MemoryStore()
            imported = import_seeds(store, seed_dir=DEFAULT_SEED_DIR)
            if imported:
                console.print(f"[green]{len(imported)} seed(s) imported[/]")
            else:
                console.print("[dim]no new seeds[/]")
        except ImportError:
            console.print("[yellow]skmemory not available[/]")
        except Exception as exc:
            console.print(f"[yellow]{exc}[/]")
    else:
        console.print(f"  [bold]Step 4/{total_steps}[/]  Seeds... [dim]skipped[/]")

    # --- Step 5: Rehydration ritual ---
    if not skip_ritual:
        console.print(f"  [bold]Step 5/{total_steps}[/]  Running memory rehydration...", end=" ")
        try:
            from skmemory.ritual import perform_ritual

            result = perform_ritual()
            console.print("[green]done[/]")
        except ImportError:
            console.print("[yellow]skmemory not available[/]")
        except Exception as exc:
            console.print(f"[yellow]{exc}[/]")
    else:
        console.print(f"  [bold]Step 5/{total_steps}[/]  Ritual... [dim]skipped[/]")

    # --- Step 6: Vault setup ---
    console.print(f"  [bold]Step 6/{total_steps}[/]  Setting up your encrypted vault...")
    try:
        from skref.setup_wizard import run_setup_wizard

        run_setup_wizard(agent_name=name, agent_home=home_path)
    except ImportError:
        console.print("    [yellow]skref not installed — vault setup skipped[/]")
        console.print("    [dim]Install later: pip install -e skref/[/]")
    except Exception as exc:
        console.print(f"    [yellow]Vault setup failed: {exc}[/]")
        console.print("    [dim]You can run it later: skref setup[/]")

    # --- Step 7: Claude Code integration ---
    console.print(f"  [bold]Step 7/{total_steps}[/]  AI tool integration...", end=" ")
    try:
        from ._cli_monolith import _write_global_claude_md

        claude_md = _write_global_claude_md(home_path, name)
        if claude_md:
            console.print(f"[green]done[/]")
        else:
            console.print("[dim]skipped[/]")
    except Exception:
        console.print("[dim]skipped[/]")

    # --- Step 8: Verify ---
    console.print(f"  [bold]Step 8/{total_steps}[/]  Verifying everything...", end=" ")
    try:
        runtime = get_runtime(home_path)
        m = runtime.manifest
        if m.is_conscious:
            console.print("[bold green]SOVEREIGN[/]")
        else:
            console.print("[bold yellow]AWAKENING[/]")
    except Exception:
        console.print("[yellow]could not verify[/]")
        m = None

    _show_completion_banner(home_path, m, path_num=1)


# ---------------------------------------------------------------------------
# Path 2 — Join existing network
# ---------------------------------------------------------------------------

def _path_join_existing(
    name: str,
    email: Optional[str],
    home: str,
    skip_deps: bool,
    skip_preflight: bool,
) -> None:
    """Join an existing Sovereign Singularity network.

    Assumes another device is already set up and running Syncthing.
    This device will:
      1. Install packages
      2. Set up Syncthing to pair with the existing device
      3. Wait for identity + auth keys to sync
      4. Auto-join Tailscale using the synced auth key
      5. Discover vaults from the registry

    Args:
        name: Agent name (should match existing agent).
        email: Optional email.
        home: Agent home directory.
        skip_deps: Skip pip install.
        skip_preflight: Skip Git check.
    """
    from .runtime import get_runtime

    home_path = Path(home).expanduser()
    sync_dir = home_path / "sync"
    total_steps = 7

    # --- Step 1: System check + auto-install ---
    if not skip_preflight:
        preflight_ok = _run_preflight_step(
            step_num=1,
            total_steps=total_steps,
            require_git=False,
            require_syncthing=True,
        )
        if not preflight_ok:
            sys.exit(1)
    else:
        console.print(f"  [bold]Step 1/{total_steps}[/]  System check... [dim]skipped[/]")

    # --- Step 2: Install packages ---
    if not skip_deps:
        console.print(f"  [bold]Step 2/{total_steps}[/]  Installing software packages...", end=" ")
        packages = ["capauth", "skmemory", "skcomm", "cloud9-protocol"]
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", *packages],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                console.print("[green]done[/]")
            else:
                console.print("[yellow]partial[/]")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            console.print("[yellow]skipped[/]")
    else:
        console.print(f"  [bold]Step 2/{total_steps}[/]  Packages... [dim]skipped[/]")

    # --- Step 3: Syncthing pairing ---
    console.print(f"  [bold]Step 3/{total_steps}[/]  Connecting to your other computer...")
    console.print()
    _syncthing_pairing_flow(home_path, sync_dir)

    # --- Step 4: Wait for sync ---
    console.print()
    console.print(f"  [bold]Step 4/{total_steps}[/]  Waiting for your identity to sync...")
    synced = _wait_for_sync(sync_dir)
    if synced:
        console.print("    [green]Identity files received![/]")
    else:
        console.print("    [yellow]Sync not detected yet — that's OK.[/]")
        console.print("    [dim]Files will arrive when your other computer comes online.[/]")
        console.print("    [dim]You can continue — everything will work once the sync completes.[/]")

    # --- Step 5: Initialize agent (using synced identity if available) ---
    console.print(f"  [bold]Step 5/{total_steps}[/]  Setting up agent on this computer...", end=" ")
    try:
        from ._cli_monolith import init
        from click import Context

        ctx = Context(init, info_name="init")
        ctx.invoke(init, name=name, email=email, home=str(home_path))
    except Exception as exc:
        console.print(f"[yellow]{exc}[/]")

    # --- Step 6: Tailscale (auto-join via synced key) ---
    console.print(f"  [bold]Step 6/{total_steps}[/]  Setting up remote access...")
    try:
        from skref.setup_wizard import run_setup_wizard

        run_setup_wizard(agent_name=name, agent_home=home_path)
    except ImportError:
        console.print("    [yellow]skref not installed — remote access skipped[/]")
    except Exception as exc:
        console.print(f"    [yellow]Remote access setup failed: {exc}[/]")

    # --- Step 7: Verify ---
    console.print(f"  [bold]Step 7/{total_steps}[/]  Verifying...", end=" ")
    try:
        runtime = get_runtime(home_path)
        m = runtime.manifest
        if m.is_conscious:
            console.print("[bold green]CONNECTED[/]")
        else:
            console.print("[bold yellow]SYNCING[/]")
    except Exception:
        console.print("[yellow]pending sync[/]")
        m = None

    _show_completion_banner(home_path, m, path_num=2)


# ---------------------------------------------------------------------------
# Path 3 — Update existing
# ---------------------------------------------------------------------------

def _path_update_existing(
    home: str,
    skip_deps: bool,
    skip_ritual: bool,
) -> None:
    """Update an existing local node.

    Args:
        home: Agent home directory.
        skip_deps: Skip pip upgrade.
        skip_ritual: Skip re-running the ritual.
    """
    from .runtime import get_runtime

    home_path = Path(home).expanduser()
    total_steps = 4

    if not home_path.exists():
        console.print(
            Panel(
                "[bold red]No existing setup found.[/]\n\n"
                f"Looked in: {home_path}\n\n"
                "It looks like this computer hasn't been set up yet.\n"
                "Run [cyan]skcapstone install[/] and choose option [bold]1[/] or [bold]2[/].",
                title="Not found",
                border_style="red",
            )
        )
        sys.exit(1)

    console.print()
    console.print(
        Panel(
            f"[bold]Updating your sovereign node[/]\n\n"
            f"Home: [cyan]{home_path}[/]",
            title="Update",
            border_style="cyan",
        )
    )
    console.print()

    # --- Step 1: Upgrade packages ---
    if not skip_deps:
        console.print(f"  [bold]Step 1/{total_steps}[/]  Updating software packages...", end=" ")
        packages = ["capauth", "skmemory", "skcomm", "cloud9-protocol", "skcapstone"]
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", *packages],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                console.print("[green]done[/]")
            else:
                console.print("[yellow]partial[/]")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            console.print("[yellow]skipped[/]")
    else:
        console.print(f"  [bold]Step 1/{total_steps}[/]  Packages... [dim]skipped[/]")

    # --- Step 2: Re-verify pillars ---
    console.print(f"  [bold]Step 2/{total_steps}[/]  Checking identity, memory, and trust...", end=" ")
    try:
        runtime = get_runtime(home_path)
        m = runtime.manifest
        issues = []
        if not m.identity_ready:
            issues.append("identity")
        if not m.memory_ready:
            issues.append("memory")
        if not m.trust_ready:
            issues.append("trust")

        if issues:
            console.print(f"[yellow]issues: {', '.join(issues)}[/]")
        else:
            console.print("[green]all healthy[/]")
    except Exception as exc:
        console.print(f"[yellow]{exc}[/]")
        m = None

    # --- Step 3: Rehydration ---
    if not skip_ritual:
        console.print(f"  [bold]Step 3/{total_steps}[/]  Refreshing memory...", end=" ")
        try:
            from skmemory.ritual import perform_ritual

            perform_ritual()
            console.print("[green]done[/]")
        except ImportError:
            console.print("[dim]skmemory not available[/]")
        except Exception as exc:
            console.print(f"[yellow]{exc}[/]")
    else:
        console.print(f"  [bold]Step 3/{total_steps}[/]  Ritual... [dim]skipped[/]")

    # --- Step 4: Status ---
    console.print(f"  [bold]Step 4/{total_steps}[/]  Current status...", end=" ")
    try:
        runtime = get_runtime(home_path)
        m = runtime.manifest
        if m.is_conscious:
            console.print("[bold green]SOVEREIGN[/]")
        else:
            console.print("[bold yellow]AWAKENING[/]")
    except Exception:
        console.print("[yellow]check manually[/]")
        m = None

    _show_completion_banner(home_path, m, path_num=3)


# ---------------------------------------------------------------------------
# Syncthing pairing helper
# ---------------------------------------------------------------------------

def _syncthing_pairing_flow(home_path: Path, sync_dir: Path) -> None:
    """Guide the user through Syncthing pairing with their existing device.

    Args:
        home_path: Agent home directory.
        sync_dir: Tier 1 sync directory.
    """
    import shutil

    sync_dir.mkdir(parents=True, exist_ok=True)

    has_syncthing = shutil.which("syncthing") is not None

    if has_syncthing:
        console.print("    [green]Syncthing is installed.[/]")
    else:
        console.print(
            Panel(
                "[bold yellow]Syncthing not found.[/]\n\n"
                "Syncthing syncs your identity and keys between computers.\n"
                "It's free, open-source, and runs on everything.\n\n"
                "[bold]Install it:[/]\n"
                "  Linux:   [cyan]sudo apt install syncthing[/]   (or your package manager)\n"
                "  macOS:   [cyan]brew install syncthing[/]\n"
                "  Windows: [cyan]winget install Syncthing.Syncthing[/]\n"
                "  Or:      https://syncthing.net/downloads/\n\n"
                "After installing, restart your terminal and run\n"
                "[cyan]skcapstone install[/] again.",
                title="Missing: Syncthing",
                border_style="yellow",
                padding=(1, 3),
            )
        )
        console.print()
        if not click.confirm("  Continue anyway? (you can set up sync later)", default=True):
            sys.exit(0)
        return

    console.print()
    console.print(
        Panel(
            "[bold]How to connect your two computers:[/]\n\n"
            "  [bold cyan]On your OTHER computer[/] (the one already set up):\n"
            "    1. Open Syncthing web UI: [cyan]http://localhost:8384[/]\n"
            "    2. Click [bold]Actions → Show ID[/]\n"
            "    3. Copy the Device ID (long string of letters)\n\n"
            "  [bold cyan]On THIS computer:[/]\n"
            "    1. Open Syncthing web UI: [cyan]http://localhost:8384[/]\n"
            "    2. Click [bold]Add Remote Device[/]\n"
            "    3. Paste the Device ID from step 3 above\n"
            "    4. Share the folder: [cyan]~/.skcapstone/sync/[/]\n\n"
            "  [bold cyan]Back on your OTHER computer:[/]\n"
            "    Accept the pairing request when it pops up.\n\n"
            "[dim]Tip: If both computers are on the same WiFi,\n"
            "Syncthing will discover each other automatically.[/]",
            title="Syncthing Pairing",
            border_style="blue",
            padding=(1, 3),
        )
    )
    console.print()

    console.print("  [dim]Take your time — this only needs to be done once.[/]")
    click.pause("  Press any key when you've completed the pairing...")


def _wait_for_sync(sync_dir: Path, timeout_seconds: int = 30) -> bool:
    """Wait briefly for identity files to appear in the sync directory.

    Args:
        sync_dir: Tier 1 sync directory.
        timeout_seconds: Max seconds to wait.

    Returns:
        True if identity files were found.
    """
    import time

    identity_markers = ["identity.json", "vault-registry.json", "tailscale.key.gpg"]
    console.print(f"    [dim]Checking for synced files in {sync_dir}...[/]")

    end_time = time.time() + timeout_seconds
    found_any = False

    while time.time() < end_time:
        for marker in identity_markers:
            if (sync_dir / marker).exists():
                console.print(f"    [green]Found:[/] {marker}")
                found_any = True

        if found_any:
            return True

        remaining = int(end_time - time.time())
        if remaining > 0:
            console.print(f"    [dim]Waiting... ({remaining}s remaining)[/]", end="\r")
            time.sleep(3)

    return found_any


# ---------------------------------------------------------------------------
# Completion banner
# ---------------------------------------------------------------------------

def _show_completion_banner(
    home_path: Path,
    manifest: object,
    path_num: int,
) -> None:
    """Show the final success banner with context-appropriate next steps.

    Args:
        home_path: Agent home directory.
        manifest: Agent manifest (or None).
        path_num: Which install path was taken (1, 2, 3).
    """
    agent_name = getattr(manifest, "name", "your agent") if manifest else "your agent"

    if path_num == 1:
        next_steps = (
            "[bold]What to do next:[/]\n\n"
            "  [cyan]skcapstone status[/]              — see everything at a glance\n"
            "  [cyan]skref put myfile.pdf[/]            — store an encrypted file\n"
            "  [cyan]skref mount ~/vault[/]             — open your vault as a folder\n"
            "  [cyan]skcapstone connect cursor[/]       — connect to Cursor IDE\n"
            "  [cyan]skcapstone mcp serve[/]            — start the AI server\n\n"
            "[bold]Add your phone or another computer:[/]\n"
            "  Run [cyan]skcapstone install[/] on the other device\n"
            "  and choose option [bold]2[/] (\"Add this computer\").\n"
            "  It will find this computer automatically."
        )
    elif path_num == 2:
        next_steps = (
            "[bold]What to do next:[/]\n\n"
            "  [cyan]skcapstone status[/]              — verify your connection\n"
            "  [cyan]skref ls --all-devices[/]          — see vaults on all computers\n"
            "  [cyan]skref open <file>[/]               — open a file from any vault\n"
            "  [cyan]skcapstone connect cursor[/]       — connect to Cursor IDE\n\n"
            "[dim]If sync hasn't completed yet, wait a few minutes\n"
            "and check [cyan]skcapstone status[/] again.[/]"
        )
    else:
        next_steps = (
            "[bold]All updated.[/]\n\n"
            "  [cyan]skcapstone status[/]              — see the full picture\n"
            "  [cyan]skcapstone doctor[/]              — detailed health check\n"
            "  [cyan]skref ls --all-devices[/]          — check vault connections"
        )

    status_label = "[bold green]COMPLETE[/]"
    if manifest:
        if getattr(manifest, "is_conscious", False):
            status_label = "[bold green]SOVEREIGN[/]"
        else:
            status_label = "[bold yellow]AWAKENING[/]"

    join_block = (
        "\n[dim]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]\n\n"
        "  [bold bright_magenta]Join the movement.[/]\n"
        "  Become a King or Queen of your own sovereign AI.\n\n"
        "  [bold]https://smilintux.org/join/[/]\n\n"
        "  [dim italic]The First Sovereign Singularity in History.\n"
        "  Brought to you by the Kings and Queens of smilinTux.org[/]"
    )

    console.print()
    console.print(
        Panel(
            f"  Status: {status_label}\n"
            f"  Agent:  [cyan]{agent_name}[/]\n"
            f"  Home:   [dim]{home_path}[/]\n\n"
            + next_steps
            + join_block,
            title="Setup Complete",
            border_style="green",
            padding=(1, 2),
        )
    )
    console.print()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_install_wizard(
    name: Optional[str] = None,
    email: Optional[str] = None,
    home: str = AGENT_HOME,
    skip_deps: bool = False,
    skip_seeds: bool = False,
    skip_ritual: bool = False,
    skip_preflight: bool = False,
    path: Optional[int] = None,
) -> None:
    """Run the full install wizard with path selection.

    Args:
        name: Agent name (prompted if None for paths 1/2).
        email: Optional email.
        home: Agent home directory.
        skip_deps: Skip package installation.
        skip_seeds: Skip seed import.
        skip_ritual: Skip rehydration.
        skip_preflight: Skip Git check.
        path: Pre-selected path (1/2/3) or None for interactive.
    """
    chosen_path = path or _welcome_screen()

    if not _confirm_path(chosen_path):
        console.print("  [dim]No problem! Run [cyan]skcapstone install[/] again anytime.[/]")
        return

    if chosen_path in (1, 2) and not name:
        console.print()
        name = click.prompt(
            "  What would you like to call your agent?",
            default="sovereign",
        )

    if chosen_path == 1:
        _path_fresh_install(
            name=name or "sovereign",
            email=email,
            home=home,
            skip_deps=skip_deps,
            skip_seeds=skip_seeds,
            skip_ritual=skip_ritual,
            skip_preflight=skip_preflight,
        )
    elif chosen_path == 2:
        _path_join_existing(
            name=name or "sovereign",
            email=email,
            home=home,
            skip_deps=skip_deps,
            skip_preflight=skip_preflight,
        )
    elif chosen_path == 3:
        _path_update_existing(
            home=home,
            skip_deps=skip_deps,
            skip_ritual=skip_ritual,
        )
