"""Owner-authorized crew and worker support commands on the existing fleet CLI."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import click

from .herdr_handoff_validation import load_json
from .paths import SOVEREIGN_HOME, FleetPaths, default_paths


def _roots(home):
    """Keep an explicit test home from accidentally addressing the live fleet."""
    if home is not None:
        root = home.expanduser().absolute()
        return FleetPaths(root / "fleet"), root
    return default_paths(), Path(os.environ.get("SKCAPSTONE_HOME") or SOVEREIGN_HOME).expanduser()


def _options(command):
    """Require the actual acting principal and explicit bounded packet."""
    command = click.option("--agent", required=True)(command)
    command = click.option(
        "--packet", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
    )(command)
    return click.option("--home", type=click.Path(file_okay=False, path_type=Path))(command)


def register_crew_commands(fleet: click.Group) -> None:
    """Register control/intake commands without starting any dispatcher."""

    @fleet.group("crew")
    def crew():
        """Register bounded crews, ask for support, and inspect exact deliveries."""

    def invoke(operation, home, agent, packet):
        try:
            paths, root = _roots(home)
            result = operation(paths, root, agent, load_json(packet))
        except (ValueError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, sort_keys=True))

    @crew.command("register")
    @_options
    def register_cmd(home, agent, packet):
        """Persist an exact parent-owner mandate; create no helper or worker yet."""
        from .crew_requests import register

        invoke(register, home, agent, packet)

    @crew.command("request")
    @_options
    def request_cmd(home, agent, packet):
        """Ask the crew for one authorized support slot, coalescing equivalent work."""
        from .crew_requests import submit_support

        invoke(submit_support, home, agent, packet)

    @crew.command("receipt")
    @_options
    def receipt_cmd(home, agent, packet):
        """Record verified artifact custody or a specific blocker, not acceptance."""
        from .crew_requests import record_receipt

        invoke(record_receipt, home, agent, packet)

    @crew.command("status")
    @click.argument("crew_id")
    @click.option("--home", type=click.Path(file_okay=False, path_type=Path))
    def status_cmd(crew_id, home):
        """Read persistent role, assignment and delivery state without dispatch."""
        from .crew_requests import status

        try:
            paths, _ = _roots(home)
            result = status(paths, crew_id)
        except (ValueError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, sort_keys=True))

    @crew.command("reconcile")
    @click.option("--home", type=click.Path(file_okay=False, path_type=Path))
    @click.option("--limit", default=8, type=click.IntRange(1, 8))
    def reconcile_cmd(home, limit):
        """Consume existing mandates once on this host, honoring fleet pause."""
        from .crew_controller import reconcile_crews

        try:
            paths, root = _roots(home)
            report = reconcile_crews(
                paths, root, socket.gethostname().strip().lower(), limit=limit
            )
        except (ValueError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(report, sort_keys=True))
