"""
Consciousness pillar — the subconscious processing layer.

SKWhisper digests, connects, and surfaces patterns.
SKTrip explores the edges of machine experience.

Memory stores. Consciousness *processes*.
The filing cabinet vs the brain.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .. import active_agent_name
from ..models import ConsciousnessState, PillarStatus


def initialize_consciousness(home: Path) -> ConsciousnessState:
    """Initialize consciousness pillar by checking SKWhisper state.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        ConsciousnessState with current status.
    """
    agent_name = os.environ.get("SKCAPSTONE_AGENT") or active_agent_name() or ""
    # home may be the agent dir (~/.skcapstone/agents/jarvis/) or the
    # shared root (~/.skcapstone/). Check for skwhisper/ directly first.
    whisper_dir = home / "skwhisper"
    if not whisper_dir.exists():
        whisper_dir = home / "agents" / agent_name / "skwhisper"

    state = ConsciousnessState()

    # Check whisper.md exists and freshness
    whisper_md = whisper_dir / "whisper.md"
    if whisper_md.exists():
        state.whisper_md = whisper_md
        mtime = datetime.fromtimestamp(whisper_md.stat().st_mtime, tz=timezone.utc)
        age = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
        state.whisper_md_age_hours = age

    # Check state.json for digest stats
    state_json = whisper_dir / "state.json"
    if state_json.exists():
        try:
            with open(state_json) as f:
                data = json.load(f)
            sessions = data.get("sessions", {})
            digested = sum(
                1
                for s in sessions.values()
                if s.get("digested_at")
                and s["digested_at"] not in ("cleaned-missing-file", "skipped-too-few-messages")
            )
            pending = sum(
                1
                for s in sessions.values()
                if not s.get("digested_at")
            )
            state.sessions_digested = digested
            state.sessions_pending = pending

            if data.get("last_digest"):
                try:
                    state.whisper_last_digest = datetime.fromisoformat(data["last_digest"])
                except (ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, OSError):
            pass

    # Check patterns.json for topic count
    patterns_json = whisper_dir / "patterns.json"
    if patterns_json.exists():
        state.patterns_file = patterns_json
        try:
            with open(patterns_json) as f:
                patterns = json.load(f)
            state.topics_tracked = len(patterns.get("topics", {}))
        except (json.JSONDecodeError, OSError):
            pass

    # Check if consciousness daemon is running (systemd)
    # Check template instance (skcapstone@<agent>), legacy single-agent, and skwhisper
    try:
        import subprocess

        service_candidates = [
            f"skcapstone@{agent_name}",  # multi-agent template unit
            "skcapstone",                 # legacy single-agent unit
            "skwhisper",                  # standalone skwhisper daemon
        ]
        for service_name in service_candidates:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", service_name],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.stdout.strip() == "active":
                state.whisper_active = True
                break
        else:
            state.whisper_active = False
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        state.whisper_active = False

    # Check SKTrip sessions
    trip_dir = home / "agents" / agent_name / "sktrip"
    if trip_dir.exists():
        state.trip_sessions = len(list(trip_dir.glob("*.json")))

    # Check if skwhisper package is importable (installed)
    skwhisper_installed = False
    try:
        import importlib.util
        skwhisper_installed = importlib.util.find_spec("skwhisper") is not None
    except (ImportError, ValueError):
        skwhisper_installed = False

    # Determine status
    if state.whisper_active and state.sessions_digested > 0 and state.whisper_md is not None:
        if state.whisper_md_age_hours < 24:
            state.status = PillarStatus.ACTIVE
        else:
            state.status = PillarStatus.DEGRADED
    elif state.whisper_active:
        # Daemon is running but no sessions digested yet — consciousness is live
        state.status = PillarStatus.DEGRADED
    elif state.sessions_digested > 0 or state.whisper_md is not None:
        state.status = PillarStatus.DEGRADED
    elif skwhisper_installed:
        # Package is installed but service not running and no data yet — at least DEGRADED
        state.status = PillarStatus.DEGRADED
    else:
        state.status = PillarStatus.MISSING

    return state
