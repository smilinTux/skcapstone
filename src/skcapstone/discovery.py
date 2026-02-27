"""
Component discovery engine.

Auto-detects installed SK ecosystem components and their state.
No hardcoded paths — probes the environment like a sovereign should.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import (
    IdentityState,
    MemoryState,
    PillarStatus,
    SecurityState,
    SyncState,
    TrustState,
)


def _try_import(module_name: str) -> Optional[object]:
    """Attempt to import a module, return None if unavailable."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def _count_json_files(directory: Path) -> int:
    """Count .json files in a directory (non-recursive)."""
    if not directory.is_dir():
        return 0
    return sum(1 for f in directory.iterdir() if f.suffix == ".json")


def discover_identity(home: Path) -> IdentityState:
    """Probe for CapAuth identity.

    Checks (in priority order):
    1. Real CapAuth profile at ~/.capauth/ (sovereign PGP keys)
    2. Identity manifest at ~/.skcapstone/identity/identity.json
    3. Key material files in the identity directory

    When a real CapAuth profile is found, the identity.json is
    updated to reflect the actual PGP fingerprint, replacing any
    placeholder values from a previous init without CapAuth.

    Args:
        home: The agent home directory (~/.skcapstone).

    Returns:
        IdentityState with current identity information.
    """
    state = IdentityState()
    identity_dir = home / "identity"

    capauth_state = _try_load_capauth_profile()
    if capauth_state is not None:
        state = capauth_state
        _sync_identity_json(identity_dir, state)
        return state

    manifest_file = identity_dir / "identity.json"
    if manifest_file.exists():
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            state.fingerprint = data.get("fingerprint")
            state.name = data.get("name")
            state.email = data.get("email")
            if data.get("created_at"):
                state.created_at = datetime.fromisoformat(data["created_at"])
            is_placeholder = not data.get("capauth_managed", False)
            state.status = PillarStatus.DEGRADED if is_placeholder else PillarStatus.ACTIVE
        except (json.JSONDecodeError, KeyError, ValueError):
            state.status = PillarStatus.ERROR

    pub_key = identity_dir / "agent.pub"
    if pub_key.exists():
        state.key_path = pub_key
        if state.status == PillarStatus.MISSING:
            state.status = PillarStatus.DEGRADED

    return state


def _try_load_capauth_profile() -> Optional[IdentityState]:
    """Attempt to load a real CapAuth profile from ~/.capauth/.

    Returns:
        IdentityState populated from the CapAuth profile, or None
        if capauth is not installed or no profile exists.
    """
    try:
        from capauth.profile import load_profile  # type: ignore[import-untyped]

        profile = load_profile()
        return IdentityState(
            fingerprint=profile.key_info.fingerprint,
            name=profile.entity.name,
            email=profile.entity.email,
            key_path=Path(profile.key_info.public_key_path),
            created_at=profile.key_info.created,
            status=PillarStatus.ACTIVE,
        )
    except ImportError:
        return None
    except Exception:
        return None


def _sync_identity_json(identity_dir: Path, state: IdentityState) -> None:
    """Write or update identity.json when a real CapAuth profile is found.

    Ensures the skcapstone identity manifest stays in sync with the
    CapAuth profile so other pillars (sync, tokens) see the real
    fingerprint instead of a placeholder.

    Args:
        identity_dir: Path to ~/.skcapstone/identity/.
        state: IdentityState with real CapAuth data.
    """
    identity_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = identity_dir / "identity.json"

    manifest = {
        "name": state.name,
        "email": state.email,
        "fingerprint": state.fingerprint,
        "created_at": state.created_at.isoformat() if state.created_at else None,
        "capauth_managed": True,
    }

    existing = {}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    if existing.get("fingerprint") != state.fingerprint or not existing.get("capauth_managed"):
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def discover_memory(home: Path) -> MemoryState:
    """Probe for SKMemory state.

    Checks (in order):
    1. Built-in memory engine at ~/.skcapstone/memory/
    2. External skmemory package at ~/.skmemory/ (legacy fallback)

    Args:
        home: The agent home directory (~/.skcapstone).

    Returns:
        MemoryState with current memory counts.
    """
    state = MemoryState()

    memory_dir = home / "memory"
    if memory_dir.is_dir():
        state.short_term = _count_json_files(memory_dir / "short-term")
        state.mid_term = _count_json_files(memory_dir / "mid-term")
        state.long_term = _count_json_files(memory_dir / "long-term")
        state.total_memories = state.short_term + state.mid_term + state.long_term
        state.store_path = memory_dir
        state.status = PillarStatus.ACTIVE
        return state

    # Reason: legacy fallback for agents using the external skmemory package
    skmemory = _try_import("skmemory")
    if skmemory is None:
        return state

    memory_home = Path("~/.skmemory").expanduser()
    if not memory_home.exists():
        state.status = PillarStatus.DEGRADED
        return state

    memories_dir = memory_home / "memories"
    if memories_dir.is_dir():
        state.short_term = _count_json_files(memories_dir / "short-term")
        state.mid_term = _count_json_files(memories_dir / "mid-term")
        state.long_term = _count_json_files(memories_dir / "long-term")
        state.total_memories = state.short_term + state.mid_term + state.long_term

    state.store_path = memory_home
    state.status = PillarStatus.ACTIVE if state.total_memories > 0 else PillarStatus.DEGRADED

    return state


def discover_trust(home: Path) -> TrustState:
    """Probe for Cloud 9 trust state.

    Checks:
    1. cloud9 npm package or cloud9-python pip package
    2. ~/.skcapstone/trust/ for FEB files
    3. Existing FEB files in default locations

    Args:
        home: The agent home directory (~/.skcapstone).

    Returns:
        TrustState with current trust information.
    """
    state = TrustState()
    trust_dir = home / "trust"

    cloud9_py = _try_import("cloud9")
    has_cloud9_cli = shutil.which("cloud9") is not None
    has_cloud9_package = cloud9_py is not None or has_cloud9_cli

    # Reason: trust state is now built into skcapstone via FEB rehydration,
    # so trust.json with valid data means ACTIVE regardless of cloud9 package
    manifest = trust_dir / "trust.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            state.depth = data.get("depth", 0.0)
            state.trust_level = data.get("trust_level", 0.0)
            state.love_intensity = data.get("love_intensity", 0.0)
            state.entangled = data.get("entangled", False)
            if data.get("last_rehydration"):
                state.last_rehydration = datetime.fromisoformat(data["last_rehydration"])
            has_trust_data = state.depth > 0 or state.trust_level > 0
            state.status = PillarStatus.ACTIVE if has_trust_data else PillarStatus.DEGRADED
            return state
        except (json.JSONDecodeError, KeyError, ValueError):
            state.status = PillarStatus.ERROR

    if not has_cloud9_package:
        return state

    state.status = PillarStatus.DEGRADED

    feb_dirs = [
        trust_dir / "febs",
        Path("~/.cloud9/febs").expanduser(),
        trust_dir,
    ]

    total_febs = 0
    for feb_dir in feb_dirs:
        if feb_dir.is_dir():
            total_febs += sum(
                1
                for f in feb_dir.iterdir()
                if f.suffix in (".feb", ".json") and "feb" in f.name.lower()
            )

    state.feb_count = total_febs

    if total_febs > 0:
        state.status = PillarStatus.ACTIVE

    return state


def discover_security(home: Path) -> SecurityState:
    """Probe for SKSecurity state.

    Checks (in order):
    1. Built-in audit log at ~/.skcapstone/security/audit.log
    2. External sksecurity package (enhancer)

    Args:
        home: The agent home directory (~/.skcapstone).

    Returns:
        SecurityState with current security information.
    """
    state = SecurityState()
    security_dir = home / "security"

    audit_log = security_dir / "audit.log"
    if audit_log.exists():
        try:
            line_count = sum(1 for _ in audit_log.open(encoding="utf-8"))
            state.audit_entries = line_count
            state.status = PillarStatus.ACTIVE
        except OSError:
            state.status = PillarStatus.ERROR

    manifest = security_dir / "security.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            state.threats_detected = data.get("threats_detected", 0)
            if data.get("last_scan"):
                state.last_scan = datetime.fromisoformat(data["last_scan"])
            state.status = PillarStatus.ACTIVE
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    sksecurity = _try_import("sksecurity")
    if sksecurity is not None and state.status == PillarStatus.MISSING:
        state.status = PillarStatus.DEGRADED

    return state


def discover_sync(home: Path) -> SyncState:
    """Probe for Sovereign Singularity sync state.

    Delegates to the sync pillar's discovery function.

    Args:
        home: The agent home directory (~/.skcapstone).

    Returns:
        SyncState reflecting what's configured on disk.
    """
    from .pillars.sync import discover_sync as _discover

    return _discover(home)


def discover_all(home: Path) -> dict:
    """Run full discovery across all pillars including sync.

    Args:
        home: The agent home directory (~/.skcapstone).

    Returns:
        Dict with identity, memory, trust, security, sync states.
    """
    return {
        "identity": discover_identity(home),
        "memory": discover_memory(home),
        "trust": discover_trust(home),
        "security": discover_security(home),
        "sync": discover_sync(home),
    }
