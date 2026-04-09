"""Session briefing helpers for SKCapstone startup flows."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context_loader import format_text, gather_context


DEFAULT_HAMMERTIME_ROOT = Path("/mnt/cloud/onedrive/projects/DAVE AI/hammerTime")


def _resolve_hammertime_root() -> Path:
    """Resolve the HammerTime workspace root."""
    return Path(os.environ.get("HAMMERTIME_ROOT", DEFAULT_HAMMERTIME_ROOT)).expanduser()


def load_hammertime_briefing(
    *,
    python_bin: str | None = None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Load the HammerTime case briefing if the repo is available."""
    if os.environ.get("SK_INCLUDE_HAMMERTIME_BRIEFING", "1") == "0":
        return None

    hammer_root = root or _resolve_hammertime_root()
    script = hammer_root / "scripts" / "case-briefing.py"
    if not script.exists():
        return None

    try:
        completed = subprocess.run(
            [python_bin or sys.executable, str(script)],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def build_session_briefing(
    home: Path,
    *,
    memory_limit: int = 10,
    python_bin: str | None = None,
) -> dict[str, Any]:
    """Build a native session briefing payload."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agent_home": str(home),
        "skcapstone_context": gather_context(home, memory_limit=memory_limit),
        "hammertime_briefing": load_hammertime_briefing(python_bin=python_bin),
    }


def format_session_briefing_text(payload: dict[str, Any]) -> str:
    """Render a human-readable session briefing."""
    lines = [
        "# SKCapstone Session Briefing",
        "",
        f"generated_at={payload.get('generated_at')}",
        f"agent_home={payload.get('agent_home')}",
        "",
        "## skcapstone context",
        format_text(payload["skcapstone_context"]).rstrip(),
    ]

    briefing = payload.get("hammertime_briefing")
    if briefing:
        top = briefing.get("top_priority") or {}
        lines.extend(
            [
                "",
                "## hammertime briefing",
                f"- alert_count: {briefing.get('alert_count', 0)}",
                f"- queue_size: {briefing.get('summary', {}).get('queue_size', 0)}",
            ]
        )
        if top:
            lines.extend(
                [
                    f"- do_this_now_incident: {top.get('incident_id')} ({top.get('problem_slug')})",
                    f"- do_this_now_action: {top.get('action')}",
                    f"- do_this_now_status: {top.get('status')}",
                ]
            )
        for item in (briefing.get("focus_items") or [])[:3]:
            lines.append(
                f"- focus: {item.get('incident_id')} -> {item.get('action')} [{item.get('status')}]"
            )
    else:
        lines.extend(["", "## hammertime briefing", "- unavailable"])

    return "\n".join(lines).rstrip() + "\n"
