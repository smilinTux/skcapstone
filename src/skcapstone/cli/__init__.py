"""
SKCapstone CLI — the sovereign agent command line.

This package organizes the CLI into modular command groups.
Each group lives in its own module for maintainability.
The main Click group is defined here and all subcommands
are registered via register functions.

Entry point: skcapstone.cli:main
"""

from __future__ import annotations

import click

from .. import __version__


@click.group()
@click.version_option(version=__version__, prog_name="skcapstone")
def main():
    """SKCapstone — Sovereign Agent Framework.

    Your agent. Everywhere. Secured. Remembering.
    """


# ---------------------------------------------------------------------------
# Register all command groups/commands from modular files
# ---------------------------------------------------------------------------

from .setup import register_setup_commands
from .status import register_status_commands
from .card import register_card_commands
from .token import register_token_commands
from .sync_cmd import register_sync_commands
from .trust import register_trust_commands
from .memory import register_memory_commands
from .coord import register_coord_commands
from .soul import register_soul_commands
from .completions import register_completions_commands
from .peer import register_peer_commands
from .backup import register_backup_commands
from .chat import register_chat_commands
from .anchor import register_anchor_commands
from .session import register_session_commands
from .context_cmd import register_context_commands
from .mcp_cmd import register_mcp_commands
from .daemon import register_daemon_commands
from .agents import register_agents_commands
from .mount import register_mount_commands

register_setup_commands(main)
register_status_commands(main)
register_card_commands(main)
register_token_commands(main)
register_sync_commands(main)
register_trust_commands(main)
register_memory_commands(main)
register_coord_commands(main)
register_soul_commands(main)
register_completions_commands(main)
register_peer_commands(main)
register_backup_commands(main)
register_chat_commands(main)
register_anchor_commands(main)
register_session_commands(main)
register_context_commands(main)
register_mcp_commands(main)
register_daemon_commands(main)
register_agents_commands(main)
register_mount_commands(main)
