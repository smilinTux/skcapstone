"""Shared qualified-tool inventory and read-only target-prefix diagnostics."""

import json
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


def _bare(host: str) -> str:
    """``node-chiap02`` and ``chiap02.tail.net`` both name host ``chiap02``."""
    return host.removeprefix("node-").split(".")[0].lower()


def is_qualification_host(home: Path, host: str) -> bool:
    """Say whether ``host`` is an authorized worker destination.

    Mirrors ``production_policy.require_destination``: a host is a destination
    when it has a ``node_quotas`` entry and, if the policy names
    ``worker_destinations``, appears in that list too. A host that is not one
    can never be handed qualified work, so it has no use for the qualify-env
    prefix, the AppArmor bwrap profile or the pinned Node.

    Fails closed: a missing, unreadable or unrecognisable policy answers True.
    """
    from .paths import paths_for_home

    try:
        policy = json.loads((paths_for_home(home).root / "production.json").read_bytes())
        quotas = policy["node_quotas"]
        destinations = policy.get("worker_destinations", quotas)
        if not isinstance(quotas, dict) or not isinstance(destinations, (list, dict)):
            return True
        name = _bare(host)
        return name in quotas and name in destinations
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return True


def qualification_applies(home: Path, host: str) -> bool:
    """Whether ``host`` must carry the qualification sandbox.

    Both must hold: the admitted node role runs qualified work
    (``builder-standby`` or ``control``, the same rule the readiness gate
    applies; no admitted spec means no qualified work) and the production
    policy authorizes the host as a worker destination. Either answer being
    undeterminable counts as "applies", so an unknown role is never exempted.
    """
    from .paths import paths_for_home

    try:
        from . import store

        spec = store.read_spec(paths_for_home(home), "node", "node-" + _bare(host))
        role = ((spec or {}).get("spec") or {}).get("role")
    except Exception:
        return True
    if role not in ("builder-standby", "control"):
        return False
    return is_qualification_host(home, host)
