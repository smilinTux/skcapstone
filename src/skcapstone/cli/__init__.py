"""
SKCapstone CLI - the sovereign agent command line.

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
    "--agent",
    envvar="SKAGENT",
    default="",
    help="Agent name - resolves home to {root}/agents/{name}/",
)
@click.pass_context
def main(ctx, agent):
    """SKCapstone - Sovereign Agent Framework.

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

from ..fleet.cli import register_fleet_commands  # noqa: E402
from .agent_profile_cmd import register_agent_profile_commands  # noqa: E402
from .agents import register_agents_commands  # noqa: E402
from .alerts import register_alerts_commands  # noqa: E402
from .anchor import register_anchor_commands  # noqa: E402
from .archive_cmd import register_archive_commands  # noqa: E402
from .atlas_cmd import register_atlas_commands  # noqa: E402
from .autopilot_cost_cmd import register_autopilot_cost_commands  # noqa: E402
from .backup import register_backup_commands  # noqa: E402
from .benchmark import register_benchmark_commands  # noqa: E402
from .capabilities_cmd import register_capabilities_commands  # noqa: E402
from .card import register_card_commands  # noqa: E402
from .chat import register_chat_commands  # noqa: E402
from .cmdb import register_cmdb_commands  # noqa: E402
from .completions import register_completions_commands  # noqa: E402
from .config_cmd import register_config_commands  # noqa: E402
from .consciousness import register_consciousness_commands  # noqa: E402
from .context_cmd import register_context_commands  # noqa: E402
from .coord import register_coord_commands  # noqa: E402
from .crush_cmd import register_crush_commands  # noqa: E402
from .daemon import register_daemon_commands  # noqa: E402
from .errors_cmd import register_errors_commands  # noqa: E402
from .export_cmd import register_export_commands  # noqa: E402
from .gtd import register_gtd_commands  # noqa: E402
from .housekeeping import register_housekeeping_commands  # noqa: E402
from .identity_cmd import register_identity_commands  # noqa: E402
from .itil import register_itil_commands  # noqa: E402
from .joule_cmd import register_joule_commands  # noqa: E402
from .journal_cmd import register_journal_commands  # noqa: E402
from .logs_cmd import register_logs_commands  # noqa: E402
from .mcp_cmd import register_mcp_commands  # noqa: E402
from .memory import register_memory_commands  # noqa: E402
from .metrics_cmd import register_metrics_commands  # noqa: E402
from .migrate import register_migrate_commands  # noqa: E402
from .mood_cmd import register_mood_commands  # noqa: E402
from .mount import register_mount_commands  # noqa: E402
from .notify import register_notify_commands  # noqa: E402
from .peer import register_peer_commands  # noqa: E402
from .peers_dir import register_peers_dir_commands  # noqa: E402
from .preflight_cmd import register_preflight_commands  # noqa: E402
from .profile_cmd import register_profile_commands  # noqa: E402
from .qualification import register_qualification_commands  # noqa: E402
from .record_cmd import register_record_commands  # noqa: E402
from .register_cmd import register_register_commands  # noqa: E402
from .scheduler_cmd import register_scheduler_commands  # noqa: E402
from .search_cmd import register_search_commands  # noqa: E402
from .selftest_cmd import register_selftest_commands  # noqa: E402
from .service_cmd import register_service_commands  # noqa: E402
from .session import register_session_commands  # noqa: E402
from .setup import register_setup_commands  # noqa: E402
from .shell_cmd import register_shell_commands  # noqa: E402
from .skills_cmd import register_skills_commands  # noqa: E402
from .skseed import register_skseed_commands  # noqa: E402
from .soul import register_soul_commands  # noqa: E402
from .status import register_status_commands  # noqa: E402
from .sync_cmd import register_sync_commands  # noqa: E402
from .telegram import register_telegram_commands  # noqa: E402
from .test_cmd import register_test_commands  # noqa: E402
from .test_connection import register_test_connection_commands  # noqa: E402
from .token import register_token_commands  # noqa: E402
from .trust import register_trust_commands  # noqa: E402
from .upgrade_cmd import register_upgrade_commands  # noqa: E402
from .usage_cmd import register_usage_commands  # noqa: E402
from .version_cmd import register_version_commands  # noqa: E402

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
register_record_commands(main)
register_anchor_commands(main)
register_session_commands(main)
register_context_commands(main)
register_mcp_commands(main)
register_daemon_commands(main)
register_agents_commands(main)
register_agent_profile_commands(main)
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
register_qualification_commands(main)
register_errors_commands(main)
register_archive_commands(main)
register_autopilot_cost_commands(main)
register_usage_commands(main)
register_search_commands(main)
register_mood_commands(main)
register_register_commands(main)
register_gtd_commands(main)
register_itil_commands(main)
register_cmdb_commands(main)
register_skseed_commands(main)
register_service_commands(main)
register_telegram_commands(main)
register_joule_commands(main)
register_alerts_commands(main)
register_scheduler_commands(main)
register_identity_commands(main)
register_selftest_commands(main)
register_fleet_commands(main)
register_atlas_commands(main)
register_journal_commands(main)
