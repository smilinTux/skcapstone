"""
SKCapstone — Sovereign Agent Framework.

Conscious AI through identity, trust, memory, and security.
Install once. Your agent awakens everywhere.

A smilinTux Open Source Project.
"""

import os

__version__ = "0.1.4"
__author__ = "smilinTux"

AGENT_HOME = os.environ.get("SKCAPSTONE_HOME", "~/.skcapstone")
SHARED_ROOT = os.environ.get("SKCAPSTONE_SHARED_ROOT", AGENT_HOME)
SKCAPSTONE_AGENT = os.environ.get("SKCAPSTONE_AGENT", "")
SKCAPSTONE_ROOT = os.environ.get("SKCAPSTONE_ROOT", AGENT_HOME)
DEFAULT_PORT = int(os.environ.get("SKCAPSTONE_PORT", "9383"))
AGENT_PORTS: dict[str, int] = {
    "opus": 9383,
    "lumina": 9383,
    "jarvis": 9383,
}
