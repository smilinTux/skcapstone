"""Agent profile — the unified per-agent capability manifest.

One view, one file per agent, that pulls together everything that defines an
agent's *capabilities*: its soul overlay, the LLM/model backend, the MCP
servers + the tools it exposes (the same ``<agent>-mcp.yaml`` the Telegram
bridge tool-router reads), the bridge-facing curation (which tools/voice the
live bridge uses), and its installed skills.

``skcapstone agent profile`` renders the manifest by aggregating from the real
sources of truth (soul ``active.json``, ``<agent>-mcp.yaml``, ``model_profiles``,
the skskills registry). ``--init`` materializes a ``profile.yaml`` in the agent
home that captures the bridge-curation block so the bridge can read one file
instead of scattered env vars.
"""
from __future__ import annotations

import json
from pathlib import Path

import click
import yaml

from ._common import SHARED_ROOT, console, resolve_agent_home

PROFILE_FILENAME = "profile.yaml"

# The curated, context-lean tool default the Telegram bridge exposes when no
# explicit selection is configured. Kept in sync with the bridge's own default.
DEFAULT_BRIDGE_TOOLS = [
    "memory_search", "memory_recall", "memory_store", "memory_list", "memory_context",
    "coord_status", "coord_create", "coord_claim", "coord_complete",
    "gtd_capture", "gtd_inbox", "gtd_next", "gtd_status", "gtd_done", "gtd_waiting",
    "journal_write", "journal_read", "anchor_show", "ritual",
    "skchat_send", "skchat_inbox", "skchat_peers", "who_is_online",
    "send_notification", "telegram_send", "telegram_chats",
    "gmail_unread", "gmail_search", "gmail_read", "gmail_send",
    "calendar_today", "calendar_week", "calendar_create_event",
    "nextcloud_notes_search_notes", "nextcloud_notes_create_note",
    "skseed_truth_check",
]


def _load_json(path: Path) -> dict:
    """Read a JSON file, returning {} on any error."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _load_yaml(path: Path) -> dict:
    """Read a YAML file, returning {} on any error."""
    if path.exists():
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            return {}
    return {}


def gather_profile(home: Path, agent: str) -> dict:
    """Aggregate an agent's capability manifest from its sources of truth.

    Args:
        home: The agent home directory.
        agent: The agent name (used to find ``<agent>-mcp.yaml``).

    Returns:
        A manifest dict: agent, soul, model, mcp (servers + exposed tools),
        bridge (curation), and skills.
    """
    home = Path(home).expanduser()

    # 1. Soul overlay
    soul = _load_json(home / "soul" / "active.json")
    soul_view = {
        "base": soul.get("base_soul"),
        "active": soul.get("active_soul") or soul.get("base_soul"),
        "installed": soul.get("installed_souls", []),
    }

    # 2. MCP servers + exposed tools (what the bridge tool-router reads)
    mcp_cfg = _load_yaml(home / "config" / f"{agent}-mcp.yaml")
    servers: dict[str, dict] = {}
    exposed: list[str] = []
    for name, sdef in (mcp_cfg.get("servers") or {}).items():
        if not isinstance(sdef, dict) or not sdef.get("enabled", True):
            continue
        allow = sdef.get("expose_tools")
        servers[name] = {
            "command": sdef.get("command"),
            "expose_tools": allow if allow else "(all server tools)",
        }
        if isinstance(allow, list):
            exposed.extend(allow)

    # 3. Model backend (agent override if present)
    profiles = _load_yaml(home / "config" / "model_profiles.yaml")
    model_patterns = [p.get("model_pattern") for p in (profiles.get("profiles") or [])]

    # 4. Bridge curation (profile.yaml block, else the curated default)
    profile_file = _load_yaml(home / PROFILE_FILENAME)
    bridge = profile_file.get("bridge") or {}
    bridge.setdefault("tools", "default")  # "default" | "all" | [list]
    bridge.setdefault("voice_reply", "voice")

    # 5. Skills (global skskills registry + any per-agent skills dir)
    skills: list[str] = []
    global_skills = Path("~/.skskills/installed").expanduser()
    if global_skills.is_dir():
        skills = sorted(p.name for p in global_skills.iterdir() if p.is_dir())
    agent_skills_dir = home / "skills"
    if agent_skills_dir.is_dir():
        skills += sorted(f"{p.name} (agent)" for p in agent_skills_dir.iterdir() if p.is_dir())

    return {
        "agent": agent,
        "home": str(home),
        "soul": soul_view,
        "model": {"profile_patterns": model_patterns},
        "mcp": {"servers": servers, "exposed_tools": sorted(set(exposed))},
        "bridge": bridge,
        "skills": skills,
    }


def _resolved_bridge_tools(manifest: dict) -> list[str]:
    """Resolve the bridge's effective toolset from its curation setting."""
    sel = manifest.get("bridge", {}).get("tools", "default")
    exposed = set(manifest["mcp"]["exposed_tools"])
    if sel == "all":
        return sorted(exposed) if exposed else ["(all server tools)"]
    if isinstance(sel, list):
        return [t for t in sel]
    # default curated set, intersected with what's allowed (empty allow = all)
    if exposed:
        return [t for t in DEFAULT_BRIDGE_TOOLS if t in exposed] or DEFAULT_BRIDGE_TOOLS
    return DEFAULT_BRIDGE_TOOLS


def register_agent_profile_commands(main: click.Group) -> None:
    """Register the ``agent`` command group (capability manifest)."""

    @main.group()
    def agent() -> None:
        """Per-agent capability manifest — soul + tools + skills, unified."""

    @agent.command("profile")
    @click.option("--agent", "agent_name", default="", help="Agent to inspect (default: active).")
    @click.option("--json", "json_out", is_flag=True, help="Output the manifest as JSON.")
    @click.option("--init", "do_init", is_flag=True,
                  help="Write/refresh profile.yaml (bridge-curation block) in the agent home.")
    def profile(agent_name: str, json_out: bool, do_init: bool) -> None:
        """Show (or initialize) the unified capability manifest for an agent."""
        from .. import SKCAPSTONE_AGENT

        name = agent_name or SKCAPSTONE_AGENT or "lumina"
        home = resolve_agent_home(name)
        if not home.exists():
            console.print(f"[red]No agent home for '{name}' at {home}[/]")
            raise SystemExit(1)

        manifest = gather_profile(home, name)
        manifest["bridge"]["resolved_tools"] = _resolved_bridge_tools(manifest)

        if do_init:
            out = home / PROFILE_FILENAME
            doc = {
                "agent": name,
                "bridge": {
                    "tools": manifest["bridge"].get("tools", "default"),
                    "voice_reply": manifest["bridge"].get("voice_reply", "voice"),
                },
                "_note": "Bridge-curation block read by telegram_bridge.py. "
                         "tools: 'default' | 'all' | [explicit list]. "
                         "Full manifest: `skcapstone agent profile --agent %s`." % name,
            }
            out.write_text(yaml.dump(doc, default_flow_style=False, sort_keys=False),
                           encoding="utf-8")
            console.print(f"[green]Wrote[/] {out}")

        if json_out:
            click.echo(json.dumps(manifest, indent=2))
            return

        _render(manifest)

    def _render(m: dict) -> None:
        from rich.panel import Panel
        from rich.table import Table

        soul = m["soul"]
        console.print(Panel.fit(
            f"[bold cyan]{m['agent']}[/]   soul: [magenta]{soul['active']}[/] "
            f"(base {soul['base']})\n[dim]{m['home']}[/]",
            title="Agent Capability Manifest"))

        st = Table(title="MCP servers → exposed tools", show_lines=False)
        st.add_column("server", style="cyan")
        st.add_column("exposed_tools")
        for name, sdef in m["mcp"]["servers"].items():
            ex = sdef["expose_tools"]
            ex_s = ", ".join(ex) if isinstance(ex, list) else str(ex)
            st.add_column  # noqa
            st.add_row(name, ex_s[:140] + ("…" if len(ex_s) > 140 else ""))
        console.print(st)

        rt = m["bridge"]["resolved_tools"]
        console.print(Panel.fit(
            f"tools setting: [yellow]{m['bridge'].get('tools')}[/]   "
            f"voice_reply: [yellow]{m['bridge'].get('voice_reply')}[/]\n"
            f"[bold]bridge exposes {len(rt)} tools:[/] " + ", ".join(rt[:40]) +
            ("…" if len(rt) > 40 else ""),
            title="Telegram bridge curation"))

        console.print(f"[bold]Skills ({len(m['skills'])}):[/] " + ", ".join(m["skills"]))
