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
@click.option(
    "--agent", envvar="SKCAPSTONE_AGENT", default="",
    help="Agent name — resolves home to {root}/agents/{name}/",
)
@click.pass_context
def main(ctx, agent):
    """SKCapstone — Sovereign Agent Framework.

    Your agent. Everywhere. Secured. Remembering.
    """
    ctx.ensure_object(dict)
    ctx.obj["agent"] = agent
    if agent:
        from ._common import apply_agent_override
        apply_agent_override(agent)


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
from .shell_cmd import register_shell_commands
from .crush_cmd import register_crush_commands
from .housekeeping import register_housekeeping_commands
from .migrate import register_migrate_commands
from .consciousness import register_consciousness_commands
from .metrics_cmd import register_metrics_commands
from .test_cmd import register_test_commands
from .notify import register_notify_commands
from .preflight_cmd import register_preflight_commands
from .peers_dir import register_peers_dir_commands
from .skills_cmd import register_skills_commands
from .capabilities_cmd import register_capabilities_commands
from .benchmark import register_benchmark_commands
from .logs_cmd import register_logs_commands
from .export_cmd import register_export_commands
from .config_cmd import register_config_commands
from .upgrade_cmd import register_upgrade_commands
from .test_connection import register_test_connection_commands
from .version_cmd import register_version_commands
from .profile_cmd import register_profile_commands
from .errors_cmd import register_errors_commands
from .archive_cmd import register_archive_commands
from .usage_cmd import register_usage_commands
from .search_cmd import register_search_commands
from .mood_cmd import register_mood_commands
from .register_cmd import register_register_commands
from .gtd import register_gtd_commands
from .itil import register_itil_commands
from .skseed import register_skseed_commands
from .service_cmd import register_service_commands
from .telegram import register_telegram_commands
from .joule_cmd import register_joule_commands

register_setup_commands(main)
register_shell_commands(main)
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
register_crush_commands(main)
register_housekeeping_commands(main)
register_migrate_commands(main)
register_consciousness_commands(main)
register_metrics_commands(main)
register_test_commands(main)
register_notify_commands(main)
register_preflight_commands(main)
register_peers_dir_commands(main)
register_skills_commands(main)
register_capabilities_commands(main)
register_logs_commands(main)
register_benchmark_commands(main)
register_export_commands(main)
register_config_commands(main)
register_upgrade_commands(main)
register_test_connection_commands(main)
register_version_commands(main)
register_profile_commands(main)
register_errors_commands(main)
register_archive_commands(main)
register_usage_commands(main)
register_search_commands(main)
register_mood_commands(main)
register_register_commands(main)
register_gtd_commands(main)
register_itil_commands(main)
register_skseed_commands(main)
register_service_commands(main)
register_telegram_commands(main)
register_joule_commands(main)
