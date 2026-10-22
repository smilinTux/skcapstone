"""Thin CLI commands for exact, receipt-bound Herdr helper handoff."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from .paths import SOVEREIGN_HOME, default_paths


def _manager(home: Path | None, agent: str):
    """Construct the authority boundary only when a command is invoked."""
    from .herdr_handoff import HandoffManager

    coordination_home = home or Path(os.environ.get("SKCAPSTONE_HOME") or SOVEREIGN_HOME)
    return HandoffManager(default_paths(), coordination_home.expanduser(), agent)


def _load(path: Path) -> dict:
    """Delegate bounded strict JSON parsing to the shared handoff contract."""
    from .herdr_handoff import load_json

    payload = load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("handoff input must be a JSON object")
    return payload


def _identity_options(command):
    """Require an explicit actor while allowing the configured coordination root."""
    command = click.option(
        "--home",
        type=click.Path(file_okay=False, path_type=Path),
        default=None,
        help="Coordination root (default: $SKCAPSTONE_HOME or ~/.skcapstone).",
    )(command)
    return click.option("--agent", required=True, help="Exact acting parent or helper owner.")(
        command
    )


def register_handoff_commands(fleet: click.Group) -> None:
    """Register the handoff group on the existing shared fleet interface."""

    @fleet.group("handoff")
    def handoff() -> None:
        """Deliver and verify an exact assignment to an existing Herdr agent."""

    @handoff.command("deliver")
    @click.option(
        "--packet",
        required=True,
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
    )
    @_identity_options
    def deliver(packet: Path, agent: str, home: Path | None) -> None:
        """Submit a bound packet once using current parent-owner authority."""
        try:
            payload = _load(packet)
            result = _manager(home, agent).deliver(payload)
        except (ValueError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, sort_keys=True))

    @handoff.command("observe")
    @click.argument("assignment")
    @_identity_options
    def observe(assignment: str, agent: str, home: Path | None) -> None:
        """Read the exact destination without retrying prompt delivery."""
        try:
            result = _manager(home, agent).observe(assignment)
        except (ValueError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, sort_keys=True))

    @handoff.command("receipt")
    @click.argument("assignment")
    @click.option("--kind", required=True, type=click.Choice(["pickup", "result"]))
    @click.option(
        "--file",
        "file_path",
        required=True,
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
    )
    @_identity_options
    def receipt(
        assignment: str, kind: str, file_path: Path, agent: str, home: Path | None
    ) -> None:
        """Validate a structured pickup or result receipt against its assignment."""
        try:
            payload = _load(file_path)
            result = _manager(home, agent).receipt(assignment, kind, payload)
        except (ValueError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, sort_keys=True))
