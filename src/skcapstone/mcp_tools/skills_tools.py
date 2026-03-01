"""SKSkills list and run tools."""

from __future__ import annotations

import logging

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _get_agent_name, _home, _json_response, _text_response

logger = logging.getLogger("skcapstone.mcp")

TOOLS: list[Tool] = [
    Tool(
        name="skskills_list_tools",
        description=(
            "List all tools available from installed SKSkills agent skills. "
            "Returns tool names in 'skill_name.tool_name' format, descriptions, "
            "and which skills are enabled or disabled. Use this to discover "
            "what skill capabilities are available before calling skskills_run_tool."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Agent namespace to load skills for (default: global)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="skskills_run_tool",
        description=(
            "Run a specific skill tool by its qualified name (skill_name.tool_name). "
            "Use skskills_list_tools first to discover available tools. "
            "Example: skskills_run_tool with tool='syncthing-setup.check_status'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "description": "Fully-qualified tool name, e.g. 'syncthing-setup.check_status'",
                },
                "args": {
                    "type": "object",
                    "description": "Arguments to pass to the tool (tool-specific)",
                },
                "agent": {
                    "type": "string",
                    "description": "Agent namespace to load skills for (default: global)",
                },
            },
            "required": ["tool"],
        },
    ),
]


async def _handle_skskills_list_tools(args: dict) -> list[TextContent]:
    """List all tools from installed SKSkills agent skills."""
    try:
        from skskills.aggregator import SkillAggregator
    except ImportError:
        return _error_response(
            "skskills is not installed. Run: pip install skskills"
        )

    agent = args.get("agent") or _get_agent_name(_home())
    agg = SkillAggregator(agent=agent)
    count = agg.load_all_skills()

    tools = agg.loader.all_tools()
    skills = agg.get_loaded_skills()

    return _json_response({
        "agent": agent,
        "skills_loaded": count,
        "skills": skills,
        "tools": [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
            }
            for t in tools
        ],
    })


async def _handle_skskills_run_tool(args: dict) -> list[TextContent]:
    """Run a specific skill tool by its qualified name."""
    try:
        from skskills.aggregator import SkillAggregator
    except ImportError:
        return _error_response(
            "skskills is not installed. Run: pip install skskills"
        )

    tool_name = args.get("tool", "")
    if not tool_name:
        return _error_response("'tool' argument is required (e.g. 'syncthing-setup.check_status')")

    agent = args.get("agent") or _get_agent_name(_home())
    tool_args = args.get("args") or {}

    agg = SkillAggregator(agent=agent)
    agg.load_all_skills()

    try:
        result = await agg.loader.call_tool(tool_name, tool_args)
        if isinstance(result, str):
            return _text_response(result)
        return _json_response(result)
    except KeyError as exc:
        return _error_response(str(exc))
    except Exception as exc:
        logger.exception("skskills_run_tool '%s' failed", tool_name)
        return _error_response(f"{tool_name} failed: {exc}")


HANDLERS: dict = {
    "skskills_list_tools": _handle_skskills_list_tools,
    "skskills_run_tool": _handle_skskills_run_tool,
}
