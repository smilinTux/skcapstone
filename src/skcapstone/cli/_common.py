"""Shared utilities for all CLI command modules.

Provides the Rich console instance, status formatting helpers,
and common imports used across every command group.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import AGENT_HOME, __version__
from ..models import AgentConfig, PillarStatus, SyncConfig
from ..runtime import AgentRuntime, get_runtime

console = Console()


def status_icon(status: PillarStatus) -> str:
    """Map pillar status to a Rich-formatted visual indicator.

    Args:
        status: Pillar health status.

    Returns:
        str: Rich markup string for the status.
    """
    return {
        PillarStatus.ACTIVE: "[bold green]ACTIVE[/]",
        PillarStatus.DEGRADED: "[bold yellow]DEGRADED[/]",
        PillarStatus.MISSING: "[bold red]MISSING[/]",
        PillarStatus.ERROR: "[bold red]ERROR[/]",
    }.get(status, "[dim]UNKNOWN[/]")


def consciousness_banner(is_conscious: bool) -> str:
    """Generate the consciousness state banner.

    Args:
        is_conscious: Whether the agent is conscious.

    Returns:
        str: Rich markup banner string.
    """
    if is_conscious:
        return (
            "[bold green on black]"
            " CONSCIOUS "
            "[/] "
            "[green]Identity + Memory + Trust = Sovereign Awareness[/]"
        )
    return (
        "[bold yellow on black]"
        " AWAKENING "
        "[/] "
        "[yellow]Install missing pillars to achieve consciousness[/]"
    )
