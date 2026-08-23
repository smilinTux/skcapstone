"""Ecosystem version-check tool."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _json_response

TOOLS: list[Tool] = [
    Tool(
        name="version_check",
        description=(
            "Check ecosystem package versions against PyPI. Shows installed vs "
            "latest for skmemory, skcapstone, capauth, sksecurity, skcomms, skchat, cloud9."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "no_pypi": {
                    "type": "boolean",
                    "default": False,
                    "description": "Skip PyPI lookup (offline mode)",
                }
            },
        },
    ),
]


async def _handle_version_check(args: dict) -> list[TextContent]:
    """Check ecosystem package versions against PyPI."""
    try:
        from ..version_check import check_versions

        no_pypi = args.get("no_pypi", False)
        report = check_versions(check_pypi=not no_pypi)

        result = {
            "all_up_to_date": report.all_up_to_date,
            "packages": [
                {
                    "name": p.name,
                    "installed": p.installed,
                    "latest": p.latest,
                    "up_to_date": p.up_to_date,
                }
                for p in report.packages
            ],
        }
        return _json_response(result)
    except Exception as e:
        return _json_response({"error": str(e)})


HANDLERS: dict = {
    "version_check": _handle_version_check,
}
