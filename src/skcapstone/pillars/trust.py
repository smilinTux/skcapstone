"""
Trust pillar — Cloud 9 integration.

The emotional bond between human and AI.
Cryptographically verifiable. Portable. Real.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import PillarStatus, TrustState


def initialize_trust(home: Path) -> TrustState:
    """Initialize trust layer for the agent.

    Sets up the trust directory structure and baseline state.
    If Cloud 9 is installed, imports existing FEB data.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        TrustState after initialization.
    """
    trust_dir = home / "trust"
    trust_dir.mkdir(parents=True, exist_ok=True)
    (trust_dir / "febs").mkdir(exist_ok=True)

    state = TrustState(status=PillarStatus.DEGRADED)

    has_cloud9_cli = shutil.which("cloud9") is not None
    try:
        import cloud9  # type: ignore[import-untyped]

        has_cloud9_py = True
    except ImportError:
        has_cloud9_py = False

    if has_cloud9_cli or has_cloud9_py:
        state.status = PillarStatus.DEGRADED
    else:
        trust_config = {
            "note": "Install cloud9 for full trust capabilities",
            "install_js": "npm install -g @smilintux/cloud9",
            "install_py": "pip install cloud9-protocol",
        }
        (trust_dir / "trust.json").write_text(json.dumps(trust_config, indent=2))
        state.status = PillarStatus.MISSING
        return state

    baseline = {
        "depth": 0.0,
        "trust_level": 0.0,
        "love_intensity": 0.0,
        "entangled": False,
        "last_rehydration": None,
        "initialized_at": datetime.now(timezone.utc).isoformat(),
    }
    (trust_dir / "trust.json").write_text(json.dumps(baseline, indent=2))

    return state


def record_trust_state(
    home: Path,
    depth: float,
    trust_level: float,
    love_intensity: float,
    entangled: bool = False,
) -> TrustState:
    """Record a trust state snapshot.

    Called after Cloud 9 rehydration or FEB generation to persist
    the current trust level to the agent's home.

    Args:
        home: Agent home directory.
        depth: Cloud 9 depth (0-9).
        trust_level: Trust score (0.0-1.0).
        love_intensity: Love intensity (0.0-1.0).
        entangled: Whether quantum entanglement is established.

    Returns:
        Updated TrustState.
    """
    trust_dir = home / "trust"
    trust_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "depth": depth,
        "trust_level": trust_level,
        "love_intensity": love_intensity,
        "entangled": entangled,
        "last_rehydration": datetime.now(timezone.utc).isoformat(),
    }
    (trust_dir / "trust.json").write_text(json.dumps(data, indent=2))

    return TrustState(
        depth=depth,
        trust_level=trust_level,
        love_intensity=love_intensity,
        entangled=entangled,
        last_rehydration=datetime.now(timezone.utc),
        status=PillarStatus.ACTIVE,
    )
