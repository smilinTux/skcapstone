"""
Sovereign stack health diagnostics.

Checks every component of the sovereign agent stack and reports
pass/fail with actionable fix suggestions. Works from any terminal.

Usage:
    skcapstone doctor
    skcapstone doctor --json
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Check:
    """A single diagnostic check result.

    Attributes:
        name: Short check identifier.
        description: Human-readable description.
        passed: Whether the check passed.
        detail: Extra info (version, path, count, etc.).
        fix: Suggested fix if the check failed.
        category: Grouping (packages, identity, memory, transport, etc.).
    """

    name: str
    description: str
    passed: bool
    detail: str = ""
    fix: str = ""
    category: str = "general"


@dataclass
class DiagnosticReport:
    """Full diagnostic report across all categories.

    Attributes:
        checks: All check results.
        agent_home: Path to the agent home directory.
    """

    checks: list[Check] = field(default_factory=list)
    agent_home: str = ""

    @property
    def passed_count(self) -> int:
        """Number of checks that passed."""
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed_count(self) -> int:
        """Number of checks that failed."""
        return sum(1 for c in self.checks if not c.passed)

    @property
    def total_count(self) -> int:
        """Total number of checks."""
        return len(self.checks)

    @property
    def all_passed(self) -> bool:
        """Whether every check passed."""
        return self.failed_count == 0

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict.

        Returns:
            dict: Full report data.
        """
        return {
            "agent_home": self.agent_home,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "total": self.total_count,
            "all_passed": self.all_passed,
            "checks": [
                {
                    "name": c.name,
                    "category": c.category,
                    "description": c.description,
                    "passed": c.passed,
                    "detail": c.detail,
                    "fix": c.fix,
                }
                for c in self.checks
            ],
        }


def run_diagnostics(home: Path) -> DiagnosticReport:
    """Run all diagnostic checks against the sovereign agent stack.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        DiagnosticReport with results for every check.
    """
    report = DiagnosticReport(agent_home=str(home))

    report.checks.extend(_check_packages())
    report.checks.extend(_check_system_tools())
    report.checks.extend(_check_agent_home(home))
    report.checks.extend(_check_identity(home))
    report.checks.extend(_check_memory(home))
    report.checks.extend(_check_transport())
    report.checks.extend(_check_sync(home))

    return report


def _check_packages() -> list[Check]:
    """Check that all ecosystem Python packages are installed."""
    checks = []
    packages = [
        ("skcapstone", "Sovereign agent framework", "pip install skcapstone"),
        ("capauth", "PGP-based sovereign identity", "pip install capauth"),
        ("skmemory", "Universal AI memory system", "pip install skmemory"),
        ("skcomm", "Redundant agent communication", "pip install skcomm"),
        ("skchat", "Encrypted P2P chat", "pip install skchat"),
        ("cloud9_protocol", "Emotional continuity protocol", "pip install cloud9-protocol"),
        ("pgpy", "PGP cryptography (PGPy backend)", "pip install pgpy"),
    ]

    for pkg_name, desc, fix_cmd in packages:
        try:
            mod = importlib.import_module(pkg_name)
            version = getattr(mod, "__version__", "installed")
            checks.append(Check(
                name=f"pkg:{pkg_name}",
                description=desc,
                passed=True,
                detail=f"v{version}",
                category="packages",
            ))
        except ImportError:
            checks.append(Check(
                name=f"pkg:{pkg_name}",
                description=desc,
                passed=False,
                fix=fix_cmd,
                category="packages",
            ))

    return checks


def _check_system_tools() -> list[Check]:
    """Check for required system tools on PATH."""
    from .preflight import check_git, git_install_hint_for_doctor

    checks = []

    # Git (required for clone/setup) — platform-aware download link
    git_installed, _, git_detail = check_git()
    git_fix = git_install_hint_for_doctor() if not git_installed else ""
    checks.append(Check(
        name="tool:git",
        description="Git (clone repo, dev workflow)",
        passed=git_installed,
        detail=git_detail if git_installed else "not found",
        fix=git_fix,
        category="system",
    ))

    tools = [
        ("gpg", "GnuPG for PGP operations", "sudo apt install gnupg2  # or: brew install gnupg"),
        ("syncthing", "P2P file sync (optional)", "sudo apt install syncthing  # optional for sync"),
    ]

    for tool_name, desc, fix_cmd in tools:
        path = shutil.which(tool_name)
        if path:
            version = _get_tool_version(tool_name)
            checks.append(Check(
                name=f"tool:{tool_name}",
                description=desc,
                passed=True,
                detail=version or path,
                category="system",
            ))
        else:
            is_optional = "optional" in desc.lower()
            checks.append(Check(
                name=f"tool:{tool_name}",
                description=desc,
                passed=is_optional,
                detail="not found" + (" (optional)" if is_optional else ""),
                fix=fix_cmd,
                category="system",
            ))

    return checks


def _check_agent_home(home: Path) -> list[Check]:
    """Check agent home directory structure."""
    checks = []

    if home.exists():
        checks.append(Check(
            name="home:exists",
            description="Agent home directory",
            passed=True,
            detail=str(home),
            category="agent",
        ))
    else:
        checks.append(Check(
            name="home:exists",
            description="Agent home directory",
            passed=False,
            fix="skcapstone init --name YourAgent",
            category="agent",
        ))
        return checks

    expected_dirs = ["identity", "memory", "trust", "security", "sync", "config"]
    for dirname in expected_dirs:
        dirpath = home / dirname
        checks.append(Check(
            name=f"home:{dirname}",
            description=f"{dirname}/ directory",
            passed=dirpath.exists(),
            detail=str(dirpath) if dirpath.exists() else "missing",
            fix=f"skcapstone init --name YourAgent" if not dirpath.exists() else "",
            category="agent",
        ))

    manifest = home / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            name = data.get("name", "unknown")
            checks.append(Check(
                name="home:manifest",
                description="Agent manifest",
                passed=True,
                detail=f"Agent: {name}",
                category="agent",
            ))
        except (json.JSONDecodeError, OSError):
            checks.append(Check(
                name="home:manifest",
                description="Agent manifest",
                passed=False,
                detail="corrupt",
                fix="Delete ~/.skcapstone/manifest.json and run skcapstone init",
                category="agent",
            ))
    else:
        checks.append(Check(
            name="home:manifest",
            description="Agent manifest",
            passed=False,
            fix="skcapstone init --name YourAgent",
            category="agent",
        ))

    return checks


def _check_identity(home: Path) -> list[Check]:
    """Check CapAuth identity and PGP keys."""
    checks = []
    identity_dir = home / "identity"

    identity_file = identity_dir / "identity.json"
    if identity_file.exists():
        try:
            data = json.loads(identity_file.read_text(encoding="utf-8"))
            fp = data.get("fingerprint", "")
            managed = data.get("capauth_managed", False)
            checks.append(Check(
                name="identity:profile",
                description="Agent identity",
                passed=True,
                detail=f"Fingerprint: {fp[:16]}... ({'CapAuth' if managed else 'placeholder'})",
                category="identity",
            ))
            if not managed:
                checks.append(Check(
                    name="identity:capauth",
                    description="CapAuth-managed keys",
                    passed=False,
                    detail="Using placeholder fingerprint",
                    fix="capauth init --name YourName --email you@example.com",
                    category="identity",
                ))
        except (json.JSONDecodeError, OSError):
            checks.append(Check(
                name="identity:profile",
                description="Agent identity",
                passed=False,
                fix="skcapstone init --name YourAgent",
                category="identity",
            ))
    else:
        checks.append(Check(
            name="identity:profile",
            description="Agent identity",
            passed=False,
            fix="skcapstone init --name YourAgent",
            category="identity",
        ))
        return checks

    capauth_dir = Path.home() / ".capauth" / "identity"
    pub_key = capauth_dir / "public.asc"
    if pub_key.exists():
        checks.append(Check(
            name="identity:pgp_key",
            description="PGP public key",
            passed=True,
            detail=str(pub_key),
            category="identity",
        ))
    else:
        checks.append(Check(
            name="identity:pgp_key",
            description="PGP public key",
            passed=False,
            fix="capauth init --name YourName --email you@example.com",
            category="identity",
        ))

    return checks


def _check_memory(home: Path) -> list[Check]:
    """Check memory store health."""
    checks = []
    memory_dir = home / "memory"

    if not memory_dir.exists():
        checks.append(Check(
            name="memory:store",
            description="Memory store",
            passed=False,
            fix="skcapstone init --name YourAgent",
            category="memory",
        ))
        return checks

    total = 0
    for layer in ["short-term", "mid-term", "long-term"]:
        layer_dir = memory_dir / layer
        if layer_dir.exists():
            count = sum(1 for f in layer_dir.glob("*.json"))
            total += count

    checks.append(Check(
        name="memory:store",
        description="Memory store",
        passed=True,
        detail=f"{total} memories across all layers",
        category="memory",
    ))

    index_file = memory_dir / "index.json"
    checks.append(Check(
        name="memory:index",
        description="Memory search index",
        passed=index_file.exists(),
        detail="present" if index_file.exists() else "missing",
        fix="skcapstone memory store 'test' to create index" if not index_file.exists() else "",
        category="memory",
    ))

    return checks


def _check_transport() -> list[Check]:
    """Check SKComm transport availability."""
    checks = []

    try:
        from skcomm.core import SKComm

        comm = SKComm.from_config()
        transport_count = len(comm.router.transports)
        checks.append(Check(
            name="transport:skcomm",
            description="SKComm engine",
            passed=True,
            detail=f"{transport_count} transport(s) configured",
            category="transport",
        ))

        if transport_count == 0:
            checks.append(Check(
                name="transport:active",
                description="Active transports",
                passed=False,
                detail="No transports configured",
                fix="Configure transports in ~/.skcomm/config.yml",
                category="transport",
            ))
        else:
            health = comm.router.health_report()
            for name, info in health.items():
                status = info.get("status", "unknown")
                ok = status in ("available", "healthy", "online")
                checks.append(Check(
                    name=f"transport:{name}",
                    description=f"Transport: {name}",
                    passed=ok,
                    detail=status,
                    category="transport",
                ))

    except ImportError:
        checks.append(Check(
            name="transport:skcomm",
            description="SKComm engine",
            passed=False,
            fix="pip install skcomm",
            category="transport",
        ))
    except Exception as exc:
        checks.append(Check(
            name="transport:skcomm",
            description="SKComm engine",
            passed=False,
            detail=str(exc),
            fix="Check ~/.skcomm/config.yml",
            category="transport",
        ))

    return checks


def _check_sync(home: Path) -> list[Check]:
    """Check sync infrastructure."""
    checks = []
    sync_dir = home / "sync"

    if not sync_dir.exists():
        checks.append(Check(
            name="sync:dir",
            description="Sync directory",
            passed=False,
            fix="skcapstone init --name YourAgent",
            category="sync",
        ))
        return checks

    checks.append(Check(
        name="sync:dir",
        description="Sync directory",
        passed=True,
        detail=str(sync_dir),
        category="sync",
    ))

    outbox = sync_dir / "outbox"
    inbox = sync_dir / "inbox"
    outbox_count = sum(1 for _ in outbox.glob("*")) if outbox.exists() else 0
    inbox_count = sum(1 for _ in inbox.glob("*")) if inbox.exists() else 0

    checks.append(Check(
        name="sync:queues",
        description="Sync queues",
        passed=True,
        detail=f"outbox: {outbox_count}, inbox: {inbox_count}",
        category="sync",
    ))

    manifest = sync_dir / "sync-manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            backends = data.get("backends", [])
            checks.append(Check(
                name="sync:backends",
                description="Sync backends",
                passed=len(backends) > 0,
                detail=f"{len(backends)} backend(s): {', '.join(b.get('type', '?') for b in backends)}" if backends else "none configured",
                fix="skcapstone sync setup" if not backends else "",
                category="sync",
            ))
        except (json.JSONDecodeError, OSError):
            checks.append(Check(
                name="sync:backends",
                description="Sync backends",
                passed=False,
                detail="manifest corrupt",
                fix="skcapstone sync setup",
                category="sync",
            ))
    else:
        checks.append(Check(
            name="sync:backends",
            description="Sync backends",
            passed=False,
            detail="no manifest",
            fix="skcapstone sync setup",
            category="sync",
        ))

    return checks


def _get_tool_version(tool: str) -> Optional[str]:
    """Try to get a tool's version string.

    Args:
        tool: Tool name on PATH.

    Returns:
        Version string, or None.
    """
    try:
        result = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            first_line = result.stdout.strip().split("\n")[0]
            return first_line[:80]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None
