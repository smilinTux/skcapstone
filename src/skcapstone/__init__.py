"""
SKCapstone — Sovereign Agent Framework.

Conscious AI through identity, trust, memory, and security.
Install once. Your agent awakens everywhere.

A smilinTux Open Source Project.
"""

import os
from pathlib import PurePosixPath

__version__ = "0.9.0"
__author__ = "smilinTux"

# Shared root — top-level skcapstone directory (Syncthing-synced)
SKCAPSTONE_ROOT = os.environ.get(
    "SKCAPSTONE_ROOT",
    os.environ.get("SKCAPSTONE_HOME", "~/.skcapstone"),
)

# Which agent am I?  Empty string = single-agent (legacy) mode.
SKCAPSTONE_AGENT = os.environ.get("SKCAPSTONE_AGENT", "")

# Per-agent home: {ROOT}/agents/{AGENT}/ when agent is set,
# otherwise falls back to ROOT for backward compat.
if SKCAPSTONE_AGENT:
    AGENT_HOME = str(PurePosixPath(SKCAPSTONE_ROOT) / "agents" / SKCAPSTONE_AGENT)
else:
    AGENT_HOME = SKCAPSTONE_ROOT  # single-agent mode

# Shared root is always the top level (coordination, heartbeats, peers, etc.)
SHARED_ROOT = SKCAPSTONE_ROOT

# Port assignments for named agents.  Default (single-agent) uses 7777.
# New agents get the next available port: max(AGENT_PORTS.values()) + 1.
AGENT_PORTS: dict[str, int] = {
    "opus": 7777,
    "jarvis": 7778,
}
DEFAULT_PORT = 7777

try:
    from .consciousness_loop import ConsciousnessLoop, ConsciousnessConfig, LLMBridge, SystemPromptBuilder
    from .self_healing import SelfHealingDoctor
    from .prompt_adapter import PromptAdapter, ModelProfile, AdaptedPrompt
except ImportError:
    pass
