"""
Uninstall wizard — clean, complete removal of a sovereign node.

Steps:
  1. Confirm the user actually wants to do this (multiple confirmations)
  2. Offer to transfer vault data to another node before wiping
  3. Deregister this device from the vault registry
  4. Disable Tailscale Funnel and log out
  5. Remove Syncthing shared folder config
  6. Delete all local data (~/.skcapstone, vaults, config)
  7. Optionally uninstall pip packages

The registry update propagates via Syncthing so other devices
stop trying to reach this node. The data transfer option copies
vault files to another device's shared folder or a cloud backend
before the local wipe.

Safety: requires typing "DELETE" to confirm. No accidental wipes.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import AGENT_HOME

console = Console()


# ---------------------------------------------------------------------------
# Inventory — what will be deleted
# ---------------------------------------------------------------------------

def _build_inventory(home_path: Path) -> dict:
    """Scan what exists on this machine and build a deletion inventory.

    Args:
        home_path: Agent home directory.

    Returns:
        Dict with keys: dirs, files, total_size_bytes, vaults, has_tailscale,
        has_syncthing, has_registry.
    """
    inventory: dict = {
        "dirs": [],
        "total_size_bytes": 0,
        "vault_names": [],
        "has_tailscale": False,
        "has_syncthing": False,
        "has_registry": False,
        "has_auth_key": False,
    }

    if home_path.exists():
        inventory["dirs"].append(str(home_path))
        inventory["total_size_bytes"] = _dir_size(home_path)

    vaults_dir = home_path / "vaults"
    if vaults_dir.exists():
        inventory["vault_names"] = [
            d.name for d in vaults_dir.iterdir() if d.is_dir()
        ]

    sync_dir = home_path / "sync"
    registry_file = sync_dir / "vault-registry.json"
    auth_key_file = sync_dir / "tailscale.key.gpg"

    inventory["has_registry"] = registry_file.exists()
    inventory["has_auth_key"] = auth_key_file.exists()
    inventory["has_tailscale"] = shutil.which("tailscale") is not None
    inventory["has_syncthing"] = shutil.which("syncthing") is not None

    # Check for vaults.yaml
    vaults_yaml = home_path / "vaults.yaml"
    if not vaults_yaml.exists():
        vaults_yaml = home_path.parent / "vaults.yaml"

    return inventory


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory in bytes."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except (OSError, PermissionError):
        pass
    return total


def _human_size(n: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Data transfer
# ---------------------------------------------------------------------------

def _offer_data_transfer(home_path: Path, inventory: dict) -> None:
    """Offer to transfer vault data to another location before wiping.

    Options:
      a) Copy to another local path (USB drive, external disk)
      b) Copy to another device's sync folder
      c) Skip (just delete)

    Args:
        home_path: Agent home directory.
        inventory: Inventory dict from _build_inventory.
    """
    vault_names = inventory["vault_names"]
    if not vault_names:
        console.print("  [dim]No vault data found — nothing to transfer.[/]")
        return

    vaults_dir = home_path / "vaults"
    vault_size = _dir_size(vaults_dir) if vaults_dir.exists() else 0

    console.print()
    console.print(
        Panel(
            "[bold]Transfer your data before deleting?[/]\n\n"
            f"You have [bold]{len(vault_names)}[/] vault(s) "
            f"({_human_size(vault_size)}) on this machine:\n"
            + "".join(f"  - {name}\n" for name in vault_names)
            + "\n"
            "You can copy them somewhere safe before wiping.\n"
            "The encrypted files can be restored on any other device.",
            title="Data Transfer",
            border_style="yellow",
            padding=(1, 3),
        )
    )
    console.print()

    console.print("  [bold]What would you like to do?[/]\n")
    console.print("    [cyan]1[/]  Copy vault data to another folder")
    console.print("       (USB drive, external disk, network share)")
    console.print()
    console.print("    [cyan]2[/]  Copy vault data to another device on your network")
    console.print("       (copies to that device's sync folder)")
    console.print()
    console.print("    [cyan]3[/]  Skip — just delete everything")
    console.print("       [dim](data is gone forever)[/]")
    console.print()

    choice = click.prompt(
        "  Your choice",
        type=click.IntRange(1, 3),
        default=3,
    )

    if choice == 1:
        _transfer_to_local(vaults_dir, vault_names)
    elif choice == 2:
        _transfer_to_device(vaults_dir, vault_names, home_path)
    else:
        console.print("  [dim]Skipping data transfer.[/]")


def _transfer_to_local(vaults_dir: Path, vault_names: list[str]) -> None:
    """Copy vault data to a user-specified local path.

    Args:
        vaults_dir: Source vaults directory.
        vault_names: List of vault names to copy.
    """
    dest = click.prompt(
        "  Destination folder (e.g. /media/usb/backup or D:\\backup)",
        type=click.Path(),
    )
    dest_path = Path(dest).expanduser()

    if not dest_path.exists():
        console.print(f"  [dim]Creating {dest_path}...[/]")
        dest_path.mkdir(parents=True, exist_ok=True)

    for name in vault_names:
        src = vaults_dir / name
        dst = dest_path / name
        if src.exists():
            console.print(f"  Copying [cyan]{name}[/]...", end=" ")
            try:
                shutil.copytree(src, dst, dirs_exist_ok=True)
                console.print("[green]done[/]")
            except (OSError, shutil.Error) as exc:
                console.print(f"[red]failed: {exc}[/]")

    console.print()
    console.print(f"  [green]Data saved to:[/] {dest_path}")
    console.print("  [dim]You can restore these vaults on another device with:[/]")
    console.print(f"  [cyan]  cp -r {dest_path}/* ~/.skcapstone/vaults/[/]")


def _transfer_to_device(
    vaults_dir: Path,
    vault_names: list[str],
    home_path: Path,
) -> None:
    """Copy vault data to another device on the network.

    Uses the vault registry to find other devices, then copies
    vault data to their Syncthing-shared folder.

    Args:
        vaults_dir: Source vaults directory.
        vault_names: Vault names to transfer.
        home_path: Agent home.
    """
    try:
        from skref.registry import load_registry
        from skref.config import load_config

        config = load_config()
        registry = load_registry()
        hostname = socket.gethostname()
        other_devices = [
            d for name, d in registry.get("devices", {}).items()
            if name != hostname and d.get("is_datastore")
        ]
    except Exception:
        other_devices = []

    if not other_devices:
        console.print("  [yellow]No other datastore devices found on your network.[/]")
        console.print("  [dim]Use option 1 to copy to a local folder instead.[/]")
        return

    console.print("  [bold]Available devices:[/]")
    for i, dev in enumerate(other_devices, 1):
        fqdn = dev.get("tailscale_fqdn", "")
        ip = dev.get("tailscale_ip", "")
        label = fqdn or ip or dev["hostname"]
        console.print(f"    [cyan]{i}[/]  {dev['hostname']} ({label})")

    idx = click.prompt(
        "  Transfer to device",
        type=click.IntRange(1, len(other_devices)),
        default=1,
    )
    target = other_devices[idx - 1]
    target_ip = target.get("tailscale_ip", "")
    target_host = target["hostname"]

    if not target_ip:
        console.print(f"  [yellow]No IP for {target_host} — try option 1 instead.[/]")
        return

    console.print(f"  Transferring to [cyan]{target_host}[/] ({target_ip})...")
    console.print("  [dim]Using rsync over Tailscale...[/]")

    for name in vault_names:
        src = vaults_dir / name
        if src.exists():
            console.print(f"  Copying [cyan]{name}[/]...", end=" ")
            try:
                dest_remote = f"{target_ip}:~/.skcapstone/vaults/{name}/"
                result = subprocess.run(
                    ["rsync", "-az", "--progress", f"{src}/", dest_remote],
                    capture_output=True, text=True, timeout=300,
                )
                if result.returncode == 0:
                    console.print("[green]done[/]")
                else:
                    console.print(f"[yellow]rsync failed — try scp or manual copy[/]")
            except FileNotFoundError:
                console.print("[yellow]rsync not found — install or use option 1[/]")
                return
            except subprocess.TimeoutExpired:
                console.print("[yellow]timed out[/]")

    console.print(f"\n  [green]Data transferred to {target_host}.[/]")


# ---------------------------------------------------------------------------
# Teardown steps
# ---------------------------------------------------------------------------

def _deregister_from_vault_registry(home_path: Path) -> None:
    """Remove this device from the vault registry.

    Args:
        home_path: Agent home directory.
    """
    console.print("  Removing this device from the vault registry...", end=" ")
    try:
        from skref.registry import deregister_device
        result = deregister_device()
        vaults_removed = result.get("vaults_removed", 0)
        console.print(f"[green]done[/] ({vaults_removed} vault(s) removed)")
        console.print("  [dim]Other devices will see this update via Syncthing.[/]")
    except ImportError:
        console.print("[dim]skref not installed — skipping[/]")
    except Exception as exc:
        console.print(f"[yellow]{exc}[/]")


def _teardown_tailscale() -> None:
    """Disable Funnel and log out of Tailscale."""
    console.print("  Disconnecting from Tailscale...", end=" ")
    try:
        from skref import tailscale
        if tailscale.is_installed():
            tailscale.logout()
            console.print("[green]logged out[/]")
            console.print(
                "  [dim]This device is no longer on your tailnet.\n"
                "  You can also remove it from the admin console:\n"
                f"  {tailscale.get_admin_console_url().replace('/keys', '/machines')}[/]"
            )
        else:
            console.print("[dim]not installed — skipping[/]")
    except ImportError:
        console.print("[dim]skref not installed — skipping[/]")
    except Exception as exc:
        console.print(f"[yellow]{exc}[/]")


def _remove_syncthing_folder(home_path: Path) -> None:
    """Remove the Syncthing shared folder config for this device.

    Args:
        home_path: Agent home directory.
    """
    sync_dir = home_path / "sync"
    console.print("  Removing Syncthing sync folder...", end=" ")

    if not shutil.which("syncthing"):
        console.print("[dim]syncthing not installed — skipping[/]")
        return

    # Syncthing REST API to remove a folder
    try:
        import urllib.request
        import json

        api_url = "http://localhost:8384/rest/config/folders"
        req = urllib.request.Request(api_url)
        resp = urllib.request.urlopen(req, timeout=5)
        folders = json.loads(resp.read())

        sync_str = str(sync_dir)
        for folder in folders:
            if sync_str in folder.get("path", ""):
                folder_id = folder["id"]
                del_url = f"http://localhost:8384/rest/config/folders/{folder_id}"
                del_req = urllib.request.Request(del_url, method="DELETE")
                urllib.request.urlopen(del_req, timeout=5)
                console.print(f"[green]removed folder '{folder_id}'[/]")
                return

        console.print("[dim]no matching folder found[/]")
    except Exception as exc:
        console.print(f"[dim]could not reach Syncthing API: {exc}[/]")
        console.print(
            "  [dim]You may need to remove the shared folder manually\n"
            "  via the Syncthing web UI: http://localhost:8384[/]"
        )


def _remove_auth_key(home_path: Path) -> None:
    """Remove the Tailscale auth key from sync folder.

    Args:
        home_path: Agent home directory.
    """
    console.print("  Removing synced auth key...", end=" ")
    try:
        from skref.tailscale import remove_auth_key
        if remove_auth_key():
            console.print("[green]removed[/]")
        else:
            console.print("[dim]not found or already removed[/]")
    except ImportError:
        auth_key = home_path / "sync" / "tailscale.key.gpg"
        if auth_key.exists():
            auth_key.unlink()
            console.print("[green]removed[/]")
        else:
            console.print("[dim]not found[/]")


def _delete_local_data(home_path: Path) -> None:
    """Delete all local sovereign data.

    Args:
        home_path: Agent home directory.
    """
    console.print("  Deleting local data...", end=" ")

    dirs_to_remove = [
        home_path,
    ]

    # Also check for vaults.yaml outside home
    vaults_yaml = home_path.parent / "vaults.yaml"
    extra_files = []
    if vaults_yaml.exists():
        extra_files.append(vaults_yaml)

    removed = 0
    for d in dirs_to_remove:
        if d.exists():
            try:
                shutil.rmtree(d)
                removed += 1
            except (OSError, PermissionError) as exc:
                console.print(f"\n    [red]Could not delete {d}: {exc}[/]")

    for f in extra_files:
        try:
            f.unlink()
        except OSError:
            pass

    if removed > 0:
        console.print(f"[green]deleted ({removed} directory)[/]")
    else:
        console.print("[dim]nothing to delete[/]")


def _uninstall_packages() -> None:
    """Offer to uninstall pip packages."""
    console.print()
    do_uninstall = click.confirm(
        "  Also uninstall the software packages? (capauth, skmemory, etc.)",
        default=False,
    )
    if not do_uninstall:
        console.print("  [dim]Packages kept. You can uninstall later with pip.[/]")
        return

    console.print("  Uninstalling packages...", end=" ")
    packages = [
        "skcapstone", "capauth", "skmemory", "skcomm",
        "cloud9-protocol", "skref", "skchat",
    ]
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", *packages],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            console.print("[green]done[/]")
        else:
            console.print("[yellow]partial[/]")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        console.print("[yellow]pip unavailable[/]")


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------

def run_uninstall_wizard(
    home: str = AGENT_HOME,
    force: bool = False,
    keep_data: bool = False,
) -> None:
    """Run the full uninstall wizard.

    Args:
        home: Agent home directory.
        force: Skip confirmations (for CI/scripting).
        keep_data: Keep local files (only deregister).
    """
    home_path = Path(home).expanduser()

    if not home_path.exists():
        console.print(
            Panel(
                "[bold]No sovereign node found.[/]\n\n"
                f"Looked in: {home_path}\n\n"
                "Nothing to uninstall.",
                title="Not Found",
                border_style="dim",
            )
        )
        return

    inventory = _build_inventory(home_path)

    # --- Show what will be removed ---
    console.print()
    console.print(
        Panel(
            "[bold red]Sovereign Node Removal[/]\n\n"
            "This will permanently remove your sovereign node\n"
            "from this computer and from your device network.\n\n"
            "[bold]This action cannot be undone.[/]",
            title="Uninstall",
            border_style="red",
            padding=(1, 3),
        )
    )
    console.print()

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Item", width=25)
    table.add_column("Details")

    table.add_row("Home directory", str(home_path))
    table.add_row("Total size", _human_size(inventory["total_size_bytes"]))

    if inventory["vault_names"]:
        table.add_row("Vaults", ", ".join(inventory["vault_names"]))
    else:
        table.add_row("Vaults", "[dim]none[/]")

    table.add_row(
        "Vault registry",
        "[green]will deregister[/]" if inventory["has_registry"] else "[dim]not found[/]",
    )
    table.add_row(
        "Tailscale",
        "[green]will log out[/]" if inventory["has_tailscale"] else "[dim]not installed[/]",
    )
    table.add_row(
        "Syncthing",
        "[green]will remove folder[/]" if inventory["has_syncthing"] else "[dim]not installed[/]",
    )
    table.add_row(
        "Auth key",
        "[green]will remove[/]" if inventory["has_auth_key"] else "[dim]not found[/]",
    )

    console.print(table)
    console.print()

    # --- Confirm ---
    if not force:
        console.print(
            "  [bold red]Are you sure?[/]\n"
            "  Type [bold]DELETE[/] to confirm (all caps):"
        )
        confirmation = click.prompt("  ", default="", show_default=False)
        if confirmation != "DELETE":
            console.print("  [green]Cancelled.[/] Nothing was changed.")
            return
        console.print()

    # --- Data transfer option ---
    if not keep_data and inventory["vault_names"]:
        _offer_data_transfer(home_path, inventory)
        console.print()

    # --- Execute teardown ---
    console.print("  [bold]Removing sovereign node...[/]")
    console.print()

    # Step 1: Deregister from vault registry (before deleting sync folder!)
    _deregister_from_vault_registry(home_path)

    # Step 2: Disable Tailscale
    if inventory["has_tailscale"]:
        _teardown_tailscale()

    # Step 3: Remove auth key from sync
    if inventory["has_auth_key"]:
        _remove_auth_key(home_path)

    # Step 4: Remove Syncthing folder config
    if inventory["has_syncthing"]:
        _remove_syncthing_folder(home_path)

    # Step 5: Delete local data
    if not keep_data:
        _delete_local_data(home_path)
    else:
        console.print("  [dim]Local data kept (--keep-data).[/]")

    # Step 6: Optionally uninstall packages
    if not force:
        _uninstall_packages()

    # --- Done ---
    console.print()
    console.print(
        Panel(
            "[bold]Sovereign node removed.[/]\n\n"
            "  This computer has been deregistered.\n"
            "  Other devices will see the update via Syncthing.\n\n"
            "  [dim]If you change your mind, run:[/]\n"
            "    [cyan]skcapstone install[/]\n"
            "  [dim]and choose option 1 (fresh setup) or 2 (rejoin).[/]\n\n"
            "[dim]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/]\n\n"
            "  [dim italic]You'll always be a King or Queen at[/]\n"
            "  [bold bright_magenta]smilinTux.org[/]\n\n"
            "  [bold]https://smilintux.org/join/[/]",
            title="Uninstall Complete",
            border_style="green",
            padding=(1, 3),
        )
    )
    console.print()
