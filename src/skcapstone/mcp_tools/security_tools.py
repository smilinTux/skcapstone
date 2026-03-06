"""SKSecurity audit and status tools.

Exposes two tools:
    security_audit_log — Read recent security audit log entries
    security_status    — Show security pillar status and config
"""

from __future__ import annotations

import json as _json

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="security_audit_log",
        description=(
            "Read recent entries from the security audit log. "
            "Returns structured JSONL entries with timestamp, event type, "
            "detail, host, and optional agent/metadata fields. "
            "Use limit to control how many entries are returned."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum entries to return (default: 20, 0 = all)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="security_status",
        description=(
            "Show the security pillar status: whether sksecurity is installed, "
            "audit log health, threat count, last scan time, and overall "
            "security configuration."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


async def _handle_security_audit_log(args: dict) -> list[TextContent]:
    """Read recent security audit log entries."""
    home = _home()
    limit = args.get("limit", 20)

    try:
        from ..pillars.security import read_audit_log

        entries = read_audit_log(home, limit=limit)
        return _json_response({
            "count": len(entries),
            "entries": [e.model_dump() for e in entries],
        })
    except Exception as exc:
        return _error_response(f"Could not read audit log: {exc}")


async def _handle_security_status(_args: dict) -> list[TextContent]:
    """Show security pillar status."""
    home = _home()
    security_dir = home / "security"
    config_file = security_dir / "security.json"

    result: dict = {"initialized": security_dir.exists()}

    if config_file.exists():
        try:
            result["config"] = _json.loads(
                config_file.read_text(encoding="utf-8")
            )
        except Exception:
            result["config"] = {"error": "could not parse security.json"}

    # Check sksecurity availability
    try:
        import sksecurity  # type: ignore[import-untyped]

        result["sksecurity_installed"] = True
        result["sksecurity_version"] = getattr(sksecurity, "__version__", "unknown")
    except ImportError:
        result["sksecurity_installed"] = False
        result["sksecurity_version"] = None

    # Audit log stats
    audit_log = security_dir / "audit.log"
    if audit_log.exists():
        try:
            lines = audit_log.read_text(encoding="utf-8").splitlines()
            result["audit_log_entries"] = len(
                [ln for ln in lines if ln.strip()]
            )
        except Exception:
            result["audit_log_entries"] = "unreadable"
    else:
        result["audit_log_entries"] = 0

    return _json_response(result)


HANDLERS: dict = {
    "security_audit_log": _handle_security_audit_log,
    "security_status": _handle_security_status,
}
