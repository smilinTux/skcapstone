"""Sync commands: push, pull, status, setup, pair, export-pubkey, import-peer-key."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.panel import Panel

from ..pillars.security import audit_event
from ..pillars.sync import discover_sync, pull_seeds, push_seed, save_sync_state
from ..runtime import get_runtime
from ._common import AGENT_HOME, console, logger, status_icon
from ._validators import validate_file_path


def _register_peer_fingerprint(home_path: Path, fingerprint: str) -> None:
    import yaml as _yaml

    config_file = home_path / "config" / "config.yaml"
    if not config_file.exists():
        return

    try:
        data = _yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        sync_data = data.setdefault("sync", {})
        peers = sync_data.setdefault("peer_fingerprints", [])
        if fingerprint not in peers:
            peers.append(fingerprint)
            config_file.write_text(_yaml.dump(data, default_flow_style=False), encoding="utf-8")
            logger.info("Registered peer fingerprint: %s", fingerprint)
    except Exception as exc:
        logger.warning("Could not persist peer fingerprint: %s", exc)


def _render_sync_audit(report) -> None:
    """Render a sync policy report as human text, with dry-run remediation.

    Args:
        report: The SyncPolicyReport from the audit run.
    """
    colors = {"error": "red", "warn": "yellow", "info": "cyan", "ok": "green"}
    console.print("\n  [bold]Syncthing private-material policy audit[/]")
    for folder in report.folders:
        console.print(
            f"\n  {folder.folder_id}  {folder.path}  "
            f"[{colors[folder.severity]}]{folder.severity.upper()}[/]"
        )
        if not folder.present_on_host:
            console.print("    [dim]not held on this host[/]")
            continue
        if not folder.findings:
            console.print("    [green](clean)[/]")
        for finding in folder.findings:
            console.print(f"    {finding.severity:5} {finding.category:32} {finding.path}")
            if finding.detail:
                console.print(f"        [dim]{finding.detail}[/]")
    for finding in report.findings:
        console.print(f"  {finding.severity:5} {finding.category:32} {finding.path}")
        if finding.detail:
            console.print(f"      [dim]{finding.detail}[/]")

    if report.dry_run:
        shown = False
        for folder in report.folders:
            lines = folder.remediation_lines
            if not lines or not folder.present_on_host:
                continue
            shown = True
            console.print(f"\n  DRY-RUN remediation for {folder.path}/.stignore (no writes):")
            for line in lines:
                console.print(f"    + {line}")
        if shown:
            console.print(
                "\n  [dim]Re-run with --apply to union-merge these rules "
                "(additive only, existing rules kept).[/]"
            )
    elif report.applied:
        console.print("\n  [green]Applied remediation (union-merge) to:[/]")
        for path in report.applied:
            console.print(f"    {path}")

    verdict = "CLEAN" if report.ok else "VIOLATIONS FOUND"
    color = "green" if report.ok else "red"
    console.print(f"\n  verdict: [{color}]{verdict}[/]\n")


def register_sync_commands(main: click.Group) -> None:
    """Register the sync command group."""

    @main.group()
    def sync():
        """Sovereign Singularity - encrypted memory sync.

        Push your agent's state to the mesh. Pull from peers.
        GPG-encrypted, Syncthing-transported, truly sovereign.
        """

    @sync.command("push")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--no-encrypt", is_flag=True, help="Skip GPG encryption.")
    def sync_push(home, no_encrypt):
        """Push current agent state to the sync mesh."""
        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        runtime = get_runtime(home_path)
        name = runtime.manifest.name

        console.print(f"\n  Collecting seed for [cyan]{name}[/]...", end=" ")
        result = push_seed(home_path, name, encrypt=not no_encrypt)

        if result:
            console.print("[green]done[/]")
            console.print(f"  [dim]Seed: {result.name}[/]")
            is_encrypted = result.suffix == ".gpg"
            if is_encrypted:
                console.print("  [green]GPG encrypted[/]")
            else:
                console.print("  [yellow]Plaintext (no GPG)[/]")

            sync_st = discover_sync(home_path)
            sync_st.last_push = datetime.now(timezone.utc)
            sync_st.seed_count = sync_st.seed_count + 1
            save_sync_state(home_path / "sync", sync_st)

            audit_event(home_path, "SYNC_PUSH", f"Seed pushed: {result.name}")
            console.print("  [dim]Syncthing will propagate to all peers.[/]\n")
        else:
            console.print("[red]failed[/]")
            sys.exit(1)

    @sync.command("pull")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    @click.option("--no-decrypt", is_flag=True, help="Skip GPG decryption.")
    def sync_pull(home, no_decrypt):
        """Pull and process seed files from peers."""
        home_path = Path(home).expanduser()
        if not home_path.exists():
            console.print("[bold red]No agent found.[/] Run skcapstone init first.")
            sys.exit(1)

        console.print("\n  Pulling seeds from inbox...", end=" ")
        seeds = pull_seeds(home_path, decrypt=not no_decrypt)

        if not seeds:
            console.print("[yellow]no new seeds[/]\n")
            return

        console.print(f"[green]{len(seeds)} seed(s) received[/]")
        for s in seeds:
            source = s.get("source_host", "unknown")
            agent = s.get("agent_name", "unknown")
            created = s.get("created_at", "unknown")
            console.print(f"    [cyan]{agent}[/]@{source} [{created}]")

        sync_st = discover_sync(home_path)
        sync_st.last_pull = datetime.now(timezone.utc)
        save_sync_state(home_path / "sync", sync_st)

        audit_event(home_path, "SYNC_PULL", f"Pulled {len(seeds)} seed(s)")
        console.print()

    @sync.command("status")
    @click.option("--home", default=AGENT_HOME, type=click.Path())
    def sync_status(home):
        """Show sync layer status and recent activity."""
        home_path = Path(home).expanduser()
        state = discover_sync(home_path)

        console.print()
        console.print(
            Panel(
                f"Transport: [cyan]{state.transport.value}[/]\n"
                f"Status: {status_icon(state.status)}\n"
                f"Seeds: [bold]{state.seed_count}[/]\n"
                f"GPG Key: {state.gpg_fingerprint or '[yellow]none[/]'}\n"
                f"Last Push: {state.last_push or '[dim]never[/]'}\n"
                f"Last Pull: {state.last_pull or '[dim]never[/]'}\n"
                f"Peers: {state.peers_known}",
                title="Sovereign Singularity",
                border_style="magenta",
            )
        )

        sync_dir = home_path / "sync"
        for folder_name in ("outbox", "inbox", "archive"):
            d = sync_dir / folder_name
            if d.exists():
                count = sum(1 for f in d.iterdir() if not f.name.startswith("."))
                console.print(f"  {folder_name}: {count} file(s)")

        console.print()

    @sync.command("setup")
    def sync_setup():
        """Set up Syncthing for sovereign P2P memory sync."""
        from ..skills.syncthing_setup import full_setup

        console.print("\n  [bold cyan]Syncthing Setup[/bold cyan]\n")
        result = full_setup()

        if not result["syncthing_installed"]:
            console.print("[yellow]Syncthing is not installed.[/yellow]\n")
            console.print(result["install_instructions"])
            console.print("\nAfter installing, run [cyan]skcapstone sync setup[/cyan] again.")
            return

        console.print("[green]Syncthing detected[/green]")

        if result["folder_configured"]:
            console.print(f"  Shared folder: [cyan]{result['folder_path']}[/cyan]")
        else:
            console.print("  [yellow]Could not configure shared folder automatically.[/]")

        if result["started"]:
            console.print("  [green]Syncthing started[/green]")
        else:
            console.print("  [yellow]Could not start Syncthing automatically.[/]")

        if result["device_id"]:
            console.print("\n  [bold]Your Device ID:[/bold]")
            console.print(f"  [cyan]{result['device_id']}[/cyan]")
            console.print("\n  Share this ID with your other device to pair.")
            console.print(
                f"  On the other device: [cyan]skcapstone sync pair {result['device_id']}[/cyan]"
            )

            if result["qr_code"]:
                console.print("\n  [bold]QR Code:[/bold]")
                console.print(result["qr_code"])
            else:
                console.print("\n  [dim]Install 'qrcode' for QR output: pip install qrcode[/dim]")
        else:
            console.print(
                "  [yellow]Could not retrieve device ID. Syncthing may still be starting.[/]"
            )

        console.print()

    @sync.command("pair")
    @click.argument("device_id")
    @click.option("--name", "-n", default="peer", help="Friendly name for the device.")
    def sync_pair(device_id, name):
        """Add a remote device for P2P sync pairing."""
        from ..skills.syncthing_setup import add_remote_device, detect_syncthing

        if not detect_syncthing():
            console.print(
                "[red]Syncthing not installed.[/] Run [cyan]skcapstone sync setup[/cyan] first."
            )
            sys.exit(1)

        console.print(f"\n  Adding device [cyan]{name}[/cyan]...")
        if add_remote_device(device_id, name):
            console.print("  [green]Device paired![/green]")
            console.print(f"  Device ID: [dim]{device_id[:20]}...[/dim]")
            console.print("  The skcapstone-sync folder is now shared with this device.")
        else:
            console.print("  [red]Failed to add device.[/] Make sure Syncthing is running.")
        console.print()

    @sync.command("export-pubkey")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option("--output", "-o", default=None, help="Output file path (default: stdout).")
    def sync_export_pubkey(home, output):
        """Export your GPG public key for sharing with peers."""
        import shutil
        import subprocess as sp

        home_path = Path(home).expanduser()

        if not shutil.which("gpg"):
            console.print("[red]gpg not found in PATH.[/]")
            sys.exit(1)

        from ..pillars.sync import _detect_gpg_key

        fingerprint = _detect_gpg_key(home_path)
        if not fingerprint:
            console.print(
                "[red]No GPG key found.[/] Run [cyan]skcapstone init[/cyan] to generate."
            )
            sys.exit(1)

        try:
            result = sp.run(
                ["gpg", "--armor", "--export", fingerprint],
                capture_output=True,
                check=True,
                timeout=15,
            )
            pubkey_data = result.stdout
        except sp.CalledProcessError as exc:
            console.print(f"[red]GPG export failed:[/] {exc}")
            sys.exit(1)

        if output:
            Path(output).write_bytes(pubkey_data)
            console.print(f"  [green]Public key exported to:[/] {output}")
            console.print(f"  [dim]Fingerprint: {fingerprint}[/]")
            console.print(
                f"  Share this file with your peer. They import it with: "
                f"[cyan]skcapstone sync import-peer-key --file {output}[/cyan]"
            )
        else:
            console.print(pubkey_data.decode("utf-8", errors="replace"))

    @sync.command("import-peer-key")
    @click.option("--home", default=AGENT_HOME, type=click.Path(), help="Agent home directory.")
    @click.option(
        "--file", "-f", "keyfile", required=True, help="Path to peer's exported public key."
    )
    @click.option("--fingerprint", default=None, help="Expected fingerprint (for verification).")
    def sync_import_peer_key(home, keyfile, fingerprint):
        """Import a peer's GPG public key and register it for encrypted sync."""
        import shutil
        import subprocess as sp

        validate_file_path(keyfile)

        home_path = Path(home).expanduser()
        key_path = Path(keyfile).expanduser()

        if not shutil.which("gpg"):
            console.print("[red]gpg not found in PATH.[/]")
            sys.exit(1)

        if not key_path.exists():
            console.print(f"[red]Key file not found:[/] {key_path}")
            sys.exit(1)

        try:
            result = sp.run(
                ["gpg", "--import", str(key_path)], capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                console.print(f"[red]GPG import failed:[/] {result.stderr.strip()}")
                sys.exit(1)
        except sp.CalledProcessError as exc:
            console.print(f"[red]GPG import error:[/] {exc}")
            sys.exit(1)

        imported_fp: Optional[str] = None
        for line in result.stderr.splitlines():
            if "key" in line.lower() and ":" in line:
                parts = line.split(":")
                for part in parts:
                    part = part.strip().replace(" ", "")
                    if len(part) in (8, 16, 40) and all(
                        c in "0123456789ABCDEFabcdef" for c in part
                    ):
                        imported_fp = part.upper()
                        break
                if imported_fp:
                    break

        if fingerprint and imported_fp and fingerprint.upper() != imported_fp:
            console.print(
                f"[yellow]Warning: expected fingerprint {fingerprint} but got {imported_fp}[/]"
            )

        if imported_fp:
            _register_peer_fingerprint(home_path, imported_fp)
            console.print(f"  [green]Peer key imported:[/] {imported_fp}")
            console.print("  Future [cyan]skcapstone sync push[/cyan] will encrypt to this peer.")
        else:
            console.print("[yellow]Key imported into GPG but fingerprint could not be parsed.[/]")
            console.print("  Run: [dim]gpg --list-keys[/dim] to find it, then add manually.")

    @sync.command("audit")
    @click.option(
        "--config",
        "config_path",
        default=None,
        type=click.Path(),
        help="Explicit Syncthing config.xml (default: standard locations under --home).",
    )
    @click.option(
        "--home",
        default=str(Path.home()),
        type=click.Path(),
        help="Home directory for config discovery and default roots.",
    )
    @click.option(
        "--agent-home",
        default=None,
        type=click.Path(),
        help="Shared agent home holding agents/*/capauth (default: <home>/.skcapstone).",
    )
    @click.option(
        "--capauth-home",
        default=None,
        type=click.Path(),
        help="Legacy capauth root to inspect (default: <home>/.capauth).",
    )
    @click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
    @click.option(
        "--apply",
        "apply_flag",
        is_flag=True,
        help="Union-merge the missing ignore rules (additive only). Default is dry-run.",
    )
    def sync_audit(config_path, home, agent_home, capauth_home, as_json, apply_flag):
        """Audit every Syncthing folder root for private-material exposure.

        Discovers each configured folder, resolves symlinks, and fails
        closed when private keys, revocation certificates, passphrases,
        secret keyrings, or token stores could synchronize. Dry-run by
        default: prints the exact .stignore lines each folder would gain
        and writes nothing. Exits nonzero on any error-grade finding.
        """
        import json as jsonlib

        from ..sync_policy import audit as run_audit

        report = run_audit(
            home=Path(home).expanduser(),
            config_path=Path(config_path).expanduser() if config_path else None,
            agent_home=Path(agent_home).expanduser() if agent_home else None,
            legacy_home=Path(capauth_home).expanduser() if capauth_home else None,
            apply=apply_flag,
        )
        if as_json:
            click.echo(jsonlib.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        else:
            _render_sync_audit(report)
        if not report.ok:
            sys.exit(1)
