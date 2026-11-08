"""Shared qualified-tool inventory and read-only target-prefix diagnostics."""

import json
import socket
import sys
from pathlib import Path

TOOL_PACKAGES = ("pytest", "_pytest", "pytest_asyncio", "ruff", "pluggy", "iniconfig", "packaging")

REQUIRED_PACKAGES = (*TOOL_PACKAGES, "ansible", "skcapstone", "skharness")

#: The clean qualification prefix, relative to the operator home. It lives
#: outside the Syncthing-shared estate tree on purpose: every host builds its
#: own copy from the same pinned lock, and production ~/.skenv is never used
#: for sealed test execution.
QUALIFY_PREFIX = Path(".local/share/skcapstone/qualify-env")

#: Deterministic build record written inside the prefix and bound into the
#: runtime and toolchain fingerprints.
STATE_NAME = "qualify-manifest.json"


def qualify_prefix(home: Path) -> Path:
    """Return the clean sealed-qualification prefix for one operator home."""
    return Path(home) / QUALIFY_PREFIX


def missing_dependencies(prefix: Path) -> list[str]:
    """Return missing prefix-contained dependencies for governed qualification."""
    site = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = site / "site-packages"
    missing = [
        name
        for name in REQUIRED_PACKAGES
        if not (site / name).is_dir()
        or not (site / name).resolve().is_relative_to(prefix.resolve())
    ]
    core_init = site / "skcapstone/__init__.py"
    if "skcapstone" not in missing and (
        not core_init.is_file() or not core_init.resolve().is_relative_to(prefix.resolve())
    ):
        missing.append("skcapstone")
    if not (prefix / "bin/ruff").is_file():
        missing.append("ruff executable")
    return missing


def _dispatch_host() -> str:
    """The host name dispatch paths compare against (not SKFLEET_NODE)."""
    return socket.gethostname().split(".")[0].lower()


def qualification_applies(home: Path, host: str | None = None) -> bool:
    """Whether this node must carry the qualification sandbox.

    Role can REQUIRE the sandbox (``builder-standby`` or ``control``) but never
    exempts: a node is exempt only when ``can_receive_qualified_work`` says no
    dispatch path could hand it qualified work, checked under both the dispatch
    host resolution and ``host`` (the SKFLEET_NODE-overridable name). A
    production.json that exists but cannot be read or parsed fails closed. A
    host with no production.json has no production placement at all, so only
    its role decides (the readiness gate's long-standing behaviour).
    """
    from .paths import paths_for_home
    from .production_policy import can_receive_qualified_work

    try:
        from . import store

        spec = store.read_spec(paths_for_home(home), "node", "node-" + _dispatch_host())
        role = ((spec or {}).get("spec") or {}).get("role")
    except Exception:
        return True
    if role in ("builder-standby", "control"):
        return True
    try:
        policy = json.loads((paths_for_home(home).root / "production.json").read_bytes())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    names = {_dispatch_host(), *([host] if host else [])}
    return any(can_receive_qualified_work(policy, name) for name in names)
