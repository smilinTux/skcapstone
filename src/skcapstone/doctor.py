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
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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
    report.checks.extend(_check_identity_consistency(home))
    report.checks.extend(_check_memory(home))
    report.checks.extend(_check_transport())
    report.checks.extend(_check_sync(home))
    report.checks.extend(_check_sync_conflicts(home))
    report.checks.extend(_check_scheduler(home))
    report.checks.extend(_check_codex())
    report.checks.extend(_check_harness_env(home))
    report.checks.extend(_check_versions())

    return report


def _check_sync_conflicts(home: Path) -> list[Check]:
    """Detect Syncthing sync-conflict files under the shared root.

    Recurring ``.sync-conflict-*`` files signal concurrent multi-node writes to
    the same synced file (root cause tracked in prb-7810b08e). Reports a count
    and the affected top-level areas. Cleanup is intentionally left to a human:
    the authoritative copy must be chosen per file, so this check warns rather
    than auto-deleting.

    Args:
        home: Shared root directory (~/.skcapstone).

    Returns:
        A single Check, passed only when no conflict files exist.
    """
    conflicts: list[Path] = []
    if home.exists():
        conflicts = [
            p
            for p in home.rglob("*.sync-conflict-*")
            if ".stversions" not in p.parts
        ]
    if not conflicts:
        return [
            Check(
                name="sync:conflicts",
                description="No Syncthing sync-conflict files",
                passed=True,
                detail="clean",
                category="sync",
            )
        ]
    areas = sorted({p.relative_to(home).parts[0] for p in conflicts})
    return [
        Check(
            name="sync:conflicts",
            description="Syncthing sync-conflict files present",
            passed=False,
            detail=f"{len(conflicts)} conflict file(s) in: {', '.join(areas)}",
            fix=(
                "List with: find ~/.skcapstone -name '*.sync-conflict-*' ; keep "
                "the authoritative copy and remove stale duplicates "
                "(root cause: prb-7810b08e)."
            ),
            category="sync",
        )
    ]


def _check_scheduler(home: Path) -> list[Check]:
    """Validate the skscheduler config (jobs.yaml) and its cron dependency.

    Args:
        home: Shared root directory (~/.skcapstone).

    Returns:
        Checks for jobs.yaml parseability (an optional file) and croniter
        availability (required for cron-style schedules).
    """
    checks: list[Check] = []
    jobs_path = home / "config" / "jobs.yaml"
    if not jobs_path.exists():
        checks.append(
            Check(
                name="scheduler:config",
                description="skscheduler jobs.yaml",
                passed=True,
                detail="not configured (optional)",
                category="system",
            )
        )
    else:
        try:
            from .scheduler_jobs import load_jobs_with_dropins

            jobs = load_jobs_with_dropins(jobs_path)
            checks.append(
                Check(
                    name="scheduler:config",
                    description="skscheduler jobs.yaml parses",
                    passed=True,
                    detail=f"{len(jobs)} job(s)",
                    category="system",
                )
            )
        except Exception as exc:  # noqa: BLE001 - report any parse failure
            checks.append(
                Check(
                    name="scheduler:config",
                    description="skscheduler jobs.yaml parse error",
                    passed=False,
                    detail=str(exc)[:120],
                    fix="Fix the YAML in ~/.skcapstone/config/jobs.yaml",
                    category="system",
                )
            )
    try:
        import croniter  # noqa: F401

        checks.append(
            Check(
                name="scheduler:croniter",
                description="croniter installed (cron schedules)",
                passed=True,
                detail="ok",
                category="system",
            )
        )
    except ImportError:
        checks.append(
            Check(
                name="scheduler:croniter",
                description="croniter missing (cron schedules unavailable)",
                passed=False,
                fix="pip install croniter",
                category="system",
            )
        )
    return checks


def _check_packages() -> list[Check]:
    """Check that all ecosystem Python packages are installed."""
    checks = []
    packages = [
        ("skcapstone", "Sovereign agent framework", "pip install skcapstone"),
        ("capauth", "PGP-based sovereign identity", "pip install capauth"),
        ("skmemory", "Universal AI memory system", "pip install skmemory"),
        ("skcomms", "Redundant agent communication", "pip install skcomms"),
        ("skchat", "Encrypted P2P chat", "pip install skchat"),
        ("cloud9", "Emotional continuity protocol", "pip install cloud9"),
        ("pgpy", "PGP cryptography (PGPy backend)", "pip install pgpy"),
    ]

    for pkg_name, desc, fix_cmd in packages:
        try:
            mod = importlib.import_module(pkg_name)
            version = getattr(mod, "__version__", "installed")
            checks.append(
                Check(
                    name=f"pkg:{pkg_name}",
                    description=desc,
                    passed=True,
                    detail=f"v{version}",
                    category="packages",
                )
            )
        except ImportError:
            checks.append(
                Check(
                    name=f"pkg:{pkg_name}",
                    description=desc,
                    passed=False,
                    fix=fix_cmd,
                    category="packages",
                )
            )
        except (ValueError, RuntimeError, OSError) as exc:
            # Package installed but failed to initialize (e.g. no agent configured)
            checks.append(
                Check(
                    name=f"pkg:{pkg_name}",
                    description=desc,
                    passed=True,
                    detail=f"installed (init pending: {exc})",
                    category="packages",
                )
            )

    return checks


def _check_system_tools() -> list[Check]:
    """Check for required system tools on PATH."""
    from .preflight import check_git, git_install_hint_for_doctor

    checks = []

    # Git (required for clone/setup) — platform-aware download link
    git_check = check_git()
    git_installed = git_check.installed
    git_detail = git_check.version or "not found"
    git_fix = git_install_hint_for_doctor() if not git_installed else ""
    checks.append(
        Check(
            name="tool:git",
            description="Git (clone repo, dev workflow)",
            passed=git_installed,
            detail=git_detail if git_installed else "not found",
            fix=git_fix,
            category="system",
        )
    )

    tools = [
        ("gpg", "GnuPG for PGP operations", "sudo apt install gnupg2  # or: brew install gnupg"),
        (
            "syncthing",
            "P2P file sync (optional)",
            "sudo apt install syncthing  # optional for sync",
        ),
    ]

    for tool_name, desc, fix_cmd in tools:
        path = shutil.which(tool_name)
        if path:
            version = _get_tool_version(tool_name)
            checks.append(
                Check(
                    name=f"tool:{tool_name}",
                    description=desc,
                    passed=True,
                    detail=version or path,
                    category="system",
                )
            )
        else:
            is_optional = "optional" in desc.lower()
            checks.append(
                Check(
                    name=f"tool:{tool_name}",
                    description=desc,
                    passed=is_optional,
                    detail="not found" + (" (optional)" if is_optional else ""),
                    fix=fix_cmd,
                    category="system",
                )
            )

    return checks


def _check_agent_home(home: Path) -> list[Check]:
    """Check agent home directory structure."""
    checks = []

    if home.exists():
        checks.append(
            Check(
                name="home:exists",
                description="Agent home directory",
                passed=True,
                detail=str(home),
                category="agent",
            )
        )
    else:
        checks.append(
            Check(
                name="home:exists",
                description="Agent home directory",
                passed=False,
                fix="skcapstone init --name YourAgent",
                category="agent",
            )
        )
        return checks

    expected_dirs = ["identity", "memory", "trust", "security", "sync", "config"]
    for dirname in expected_dirs:
        dirpath = home / dirname
        checks.append(
            Check(
                name=f"home:{dirname}",
                description=f"{dirname}/ directory",
                passed=dirpath.exists(),
                detail=str(dirpath) if dirpath.exists() else "missing",
                fix=f"skcapstone init --name YourAgent" if not dirpath.exists() else "",
                category="agent",
            )
        )

    manifest = home / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            name = data.get("name", "unknown")
            checks.append(
                Check(
                    name="home:manifest",
                    description="Agent manifest",
                    passed=True,
                    detail=f"Agent: {name}",
                    category="agent",
                )
            )
        except (json.JSONDecodeError, OSError):
            checks.append(
                Check(
                    name="home:manifest",
                    description="Agent manifest",
                    passed=False,
                    detail="corrupt",
                    fix="Delete ~/.skcapstone/manifest.json and run skcapstone init",
                    category="agent",
                )
            )
    else:
        checks.append(
            Check(
                name="home:manifest",
                description="Agent manifest",
                passed=False,
                fix="skcapstone init --name YourAgent",
                category="agent",
            )
        )

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
            checks.append(
                Check(
                    name="identity:profile",
                    description="Agent identity",
                    passed=True,
                    detail=f"Fingerprint: {fp[:16]}... ({'CapAuth' if managed else 'placeholder'})",
                    category="identity",
                )
            )
            if not managed:
                checks.append(
                    Check(
                        name="identity:capauth",
                        description="CapAuth-managed keys",
                        passed=False,
                        detail="Using placeholder fingerprint",
                        fix="capauth init --name YourName --email you@example.com",
                        category="identity",
                    )
                )
        except (json.JSONDecodeError, OSError):
            checks.append(
                Check(
                    name="identity:profile",
                    description="Agent identity",
                    passed=False,
                    fix="skcapstone init --name YourAgent",
                    category="identity",
                )
            )
    else:
        checks.append(
            Check(
                name="identity:profile",
                description="Agent identity",
                passed=False,
                fix="skcapstone init --name YourAgent",
                category="identity",
            )
        )
        return checks

    capauth_dir = Path.home() / ".capauth" / "identity"
    pub_key = capauth_dir / "public.asc"
    if pub_key.exists():
        checks.append(
            Check(
                name="identity:pgp_key",
                description="PGP public key",
                passed=True,
                detail=str(pub_key),
                category="identity",
            )
        )
    else:
        checks.append(
            Check(
                name="identity:pgp_key",
                description="PGP public key",
                passed=False,
                fix="capauth init --name YourName --email you@example.com",
                category="identity",
            )
        )

    return checks


# Agents are considered "provisioned" — and therefore expected to carry a
# per-agent identity.json — when they have a CapAuth home on disk. Empty
# scaffolds and ``*-template`` directories are intentionally excluded so the
# check does not red-flag dirs that were never meant to hold a real identity.
def _provisioned_agents(home: Path) -> list[str]:
    """List agents that have a CapAuth home (and thus a real identity).

    Args:
        home: Shared root directory (~/.skcapstone).

    Returns:
        Sorted agent names whose ``agents/<name>/capauth/`` dir exists,
        excluding ``*-template`` scaffolds.
    """
    agents_root = home / "agents"
    if not agents_root.is_dir():
        return []
    names = []
    for d in agents_root.iterdir():
        if not d.is_dir() or d.name.endswith("-template"):
            continue
        if (d / "capauth").is_dir():
            names.append(d.name)
    return sorted(names)


def _scan_capauth_local(home: Path) -> list[str]:
    """Find identity.json files still carrying an ``@capauth.local`` placeholder.

    The ``@capauth.local`` suffix was the old placeholder email minted before a
    real CapAuth profile existed. The unified identity layer (epic 2b264064)
    eliminated it; any lingering occurrence means a stale/placeholder identity
    that should be re-minted from the real profile.

    Args:
        home: Shared root directory (~/.skcapstone).

    Returns:
        Relative paths (to *home*) of identity.json files containing the
        placeholder, sorted for stable output.
    """
    candidates = [home / "identity" / "identity.json"]
    agents_root = home / "agents"
    if agents_root.is_dir():
        candidates += sorted(agents_root.glob("*/identity/identity.json"))
    hits: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            if "@capauth.local" in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(home)))
        except OSError:
            continue
    return hits


def _check_identity_consistency(home: Path) -> list[Check]:
    """Validate the unified identity layer (epic 2b264064 / skos T6).

    Locks in the single agent-aware resolver and the shared-operator /
    per-agent-wire split. Five checks in the ``identity`` category:

    1. ``identity:resolver`` — ``capauth.resolve_agent_identity`` is importable
       (the single canonical resolver every SK package delegates to).
    2. ``identity:self`` — that resolver returns an agent-aware identity for the
       active agent (not the ``"local"`` floor) with a populated ``capauth_uri``.
    3. ``identity:operator`` — the shared ``~/.skcapstone/identity/identity.json``
       describes the operator (``role == "operator"``), not a stale placeholder.
    4. ``identity:no-placeholder`` — no identity.json anywhere still carries an
       ``@capauth.local`` placeholder email.
    5. ``identity:per-agent`` — every provisioned agent (one with a CapAuth home)
       has its own per-agent ``identity/identity.json``.

    Args:
        home: Shared root directory (~/.skcapstone).

    Returns:
        Up to five Check results in the ``identity`` category.
    """
    checks: list[Check] = []

    # 1. The canonical resolver must be importable.
    resolver = None
    try:
        from capauth import resolve_agent_identity as resolver  # type: ignore
        checks.append(
            Check(
                name="identity:resolver",
                description="Unified identity resolver (capauth.resolve_agent_identity)",
                passed=True,
                detail="importable — the single canonical resolver",
                category="identity",
            )
        )
    except ImportError as exc:
        checks.append(
            Check(
                name="identity:resolver",
                description="Unified identity resolver (capauth.resolve_agent_identity)",
                passed=False,
                detail=str(exc),
                fix="pip install -e capauth  (epic 2b264064 — capauth is the source of truth)",
                category="identity",
            )
        )

    # 2. Self-identity resolves agent-aware (not the "local" floor).
    if resolver is not None:
        try:
            ident = resolver()
            aware = bool(ident.agent) and ident.agent != "local" and bool(ident.capauth_uri)
            fqid = getattr(ident, "fqid", None)
            detail = f"{ident.agent} → {ident.capauth_uri}" + (f" / {fqid}" if fqid else "")
            checks.append(
                Check(
                    name="identity:self",
                    description="Self-identity resolves agent-aware",
                    passed=aware,
                    detail=detail if aware else f"resolved to floor: {ident.agent!r}",
                    fix=(
                        ""
                        if aware
                        else "Set SKAGENT (or run `skswitch <agent>`) so the resolver "
                        "binds a real agent instead of the 'local' floor"
                    ),
                    category="identity",
                )
            )
        except Exception as exc:  # noqa: BLE001 - any resolver failure is a finding
            checks.append(
                Check(
                    name="identity:self",
                    description="Self-identity resolves agent-aware",
                    passed=False,
                    detail=str(exc)[:120],
                    fix="Investigate capauth.resolve_agent_identity() failure",
                    category="identity",
                )
            )

    # 3. Shared identity.json describes the operator.
    shared = home / "identity" / "identity.json"
    operator_ok = False
    detail = "missing"
    if shared.exists():
        try:
            data = json.loads(shared.read_text(encoding="utf-8"))
            role = (data.get("role") or "").lower()
            operator_ok = role == "operator"
            detail = (
                f"{data.get('name', '?')} (role={role or 'unset'})"
                if operator_ok
                else f"role={role or 'unset'} (expected 'operator')"
            )
        except (json.JSONDecodeError, OSError) as exc:
            detail = f"unreadable: {exc}"
    checks.append(
        Check(
            name="identity:operator",
            description="Shared identity.json = operator",
            passed=operator_ok,
            detail=detail,
            fix=(
                ""
                if operator_ok
                else "Set \"role\": \"operator\" on ~/.skcapstone/identity/identity.json "
                "(shared file is the operator; agents resolve per-agent)"
            ),
            category="identity",
        )
    )

    # 4. No @capauth.local placeholder lingers anywhere.
    placeholders = _scan_capauth_local(home)
    checks.append(
        Check(
            name="identity:no-placeholder",
            description="No @capauth.local placeholder identities",
            passed=not placeholders,
            detail="clean" if not placeholders else f"{len(placeholders)} file(s): {', '.join(placeholders)}",
            fix=(
                ""
                if not placeholders
                else "Re-mint the listed identity.json from the real CapAuth profile "
                "(remove the @capauth.local placeholder email)"
            ),
            category="identity",
        )
    )

    # 5. Every provisioned agent carries a per-agent identity.json.
    provisioned = _provisioned_agents(home)
    missing = [
        a for a in provisioned
        if not (home / "agents" / a / "identity" / "identity.json").exists()
    ]
    if not provisioned:
        checks.append(
            Check(
                name="identity:per-agent",
                description="Per-agent identity.json for provisioned agents",
                passed=True,
                detail="no provisioned agents (none with a CapAuth home)",
                category="identity",
            )
        )
    else:
        checks.append(
            Check(
                name="identity:per-agent",
                description="Per-agent identity.json for provisioned agents",
                passed=not missing,
                detail=(
                    f"{len(provisioned)} agent(s), all present"
                    if not missing
                    else f"missing for: {', '.join(missing)}"
                ),
                fix=(
                    ""
                    if not missing
                    else "Run `capauth init` for the listed agents so each has a "
                    "per-agent identity/identity.json"
                ),
                category="identity",
            )
        )

    return checks


def _resolve_memory_dir(home: Path) -> Path:
    """Resolve the memory directory for either shared-root or agent-home inputs."""
    from . import active_agent_name

    agent_name = os.environ.get("SKCAPSTONE_AGENT") or active_agent_name() or ""
    if home.parent.name == "agents":
        return home / "memory"
    if agent_name:
        return home / "agents" / agent_name / "memory"
    return home / "memory"


def _check_memory(home: Path) -> list[Check]:
    """Check memory store health."""
    checks = []
    memory_dir = _resolve_memory_dir(home)

    if not memory_dir.exists():
        checks.append(
            Check(
                name="memory:store",
                description="Memory store",
                passed=False,
                fix="skcapstone init --name YourAgent",
                category="memory",
            )
        )
        return checks

    total = 0
    for layer in ["short-term", "mid-term", "long-term"]:
        layer_dir = memory_dir / layer
        if layer_dir.exists():
            count = sum(1 for f in layer_dir.glob("*.json"))
            total += count

    checks.append(
        Check(
            name="memory:store",
            description="Memory store",
            passed=True,
            detail=f"{total} memories across all layers",
            category="memory",
        )
    )

    index_file = memory_dir / "index.json"
    checks.append(
        Check(
            name="memory:index",
            description="Memory search index",
            passed=index_file.exists(),
            detail="present" if index_file.exists() else "missing",
            fix=(
                "skcapstone memory store 'test' to create index" if not index_file.exists() else ""
            ),
            category="memory",
        )
    )

    return checks


def _check_transport() -> list[Check]:
    """Check SKComms transport availability."""
    checks = []

    try:
        from skcomms.core import SKComms

        comm = SKComms.from_config()
        transport_count = len(comm.router.transports)
        checks.append(
            Check(
                name="transport:skcomms",
                description="SKComms engine",
                passed=True,
                detail=f"{transport_count} transport(s) configured",
                category="transport",
            )
        )

        if transport_count == 0:
            checks.append(
                Check(
                    name="transport:active",
                    description="Active transports",
                    passed=False,
                    detail="No transports configured",
                    fix="Configure transports in ~/.skcomms/config.yml",
                    category="transport",
                )
            )
        else:
            health = comm.router.health_report()
            for name, info in health.items():
                status = info.get("status", "unknown")
                ok = status in ("available", "healthy", "online")
                checks.append(
                    Check(
                        name=f"transport:{name}",
                        description=f"Transport: {name}",
                        passed=ok,
                        detail=status,
                        category="transport",
                    )
                )

    except ImportError:
        checks.append(
            Check(
                name="transport:skcomms",
                description="SKComms engine",
                passed=False,
                fix="pip install skcomms",
                category="transport",
            )
        )
    except Exception as exc:
        checks.append(
            Check(
                name="transport:skcomms",
                description="SKComms engine",
                passed=False,
                detail=str(exc),
                fix="Check ~/.skcomms/config.yml",
                category="transport",
            )
        )

    return checks


def _check_sync(home: Path) -> list[Check]:
    """Check sync infrastructure."""
    checks = []
    sync_dir = home / "sync"

    if not sync_dir.exists():
        checks.append(
            Check(
                name="sync:dir",
                description="Sync directory",
                passed=False,
                fix="skcapstone init --name YourAgent",
                category="sync",
            )
        )
        return checks

    checks.append(
        Check(
            name="sync:dir",
            description="Sync directory",
            passed=True,
            detail=str(sync_dir),
            category="sync",
        )
    )

    outbox = sync_dir / "outbox"
    inbox = sync_dir / "inbox"
    outbox_count = sum(1 for _ in outbox.glob("*")) if outbox.exists() else 0
    inbox_count = sum(1 for _ in inbox.glob("*")) if inbox.exists() else 0

    checks.append(
        Check(
            name="sync:queues",
            description="Sync queues",
            passed=True,
            detail=f"outbox: {outbox_count}, inbox: {inbox_count}",
            category="sync",
        )
    )

    manifest = sync_dir / "sync-manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            backends = data.get("backends", [])
            checks.append(
                Check(
                    name="sync:backends",
                    description="Sync backends",
                    passed=len(backends) > 0,
                    detail=(
                        f"{len(backends)} backend(s): {', '.join(b.get('type', '?') for b in backends)}"
                        if backends
                        else "none configured"
                    ),
                    fix="skcapstone sync setup" if not backends else "",
                    category="sync",
                )
            )
        except (json.JSONDecodeError, OSError):
            checks.append(
                Check(
                    name="sync:backends",
                    description="Sync backends",
                    passed=False,
                    detail="manifest corrupt",
                    fix="skcapstone sync setup",
                    category="sync",
                )
            )
    else:
        checks.append(
            Check(
                name="sync:backends",
                description="Sync backends",
                passed=False,
                detail="no manifest",
                fix="skcapstone sync setup",
                category="sync",
            )
        )

    return checks


def _check_codex() -> list[Check]:
    """Check Codex global SK agent prompt bootstrap."""
    codex_detected = bool(os.environ.get("CODEX_HOME")) or (Path.home() / ".codex").exists()
    codex_detected = codex_detected or shutil.which("codex") is not None

    if not codex_detected:
        return [
            Check(
                name="codex:agent_context",
                description="Codex SK agent context bootstrap",
                passed=True,
                detail="Codex not detected (optional)",
                category="codex",
            )
        ]

    try:
        from .codex_setup import check_codex_setup

        configured, detail = check_codex_setup()
        return [
            Check(
                name="codex:agent_context",
                description="Codex SK agent context bootstrap",
                passed=configured,
                detail=detail,
                fix="" if configured else "skcapstone doctor --fix",
                category="codex",
            )
        ]
    except OSError as exc:
        return [
            Check(
                name="codex:agent_context",
                description="Codex SK agent context bootstrap",
                passed=False,
                detail=str(exc),
                fix="skcapstone doctor --fix",
                category="codex",
            )
        ]


def _check_versions() -> list[Check]:
    """Check for outdated ecosystem packages."""
    checks = []

    try:
        from .version_check import check_versions

        report = check_versions(check_pypi=True)
        for pkg in report.packages:
            if not pkg.installed:
                continue
            if pkg.up_to_date:
                continue
            checks.append(
                Check(
                    name=f"version:{pkg.name}",
                    description=f"{pkg.name} outdated ({pkg.installed} \u2192 {pkg.latest})",
                    passed=False,
                    detail=f"installed: {pkg.installed}, latest: {pkg.latest}",
                    fix=f"pip install --upgrade {pkg.name}",
                    category="packages",
                )
            )
    except Exception as exc:
        logger.warning("Version check failed (non-fatal): %s", exc)

    return checks


# ───────────────────────────────────────────────────────────────────────────
# AI-harness (Claude Code) environment checks
# ───────────────────────────────────────────────────────────────────────────


def _claude_config_home() -> Path:
    """Resolve the Claude Code config directory (honours CLAUDE_CONFIG_DIR)."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def _load_json_safe(path: Path) -> dict:
    """Load JSON from *path*, returning {} on any error (missing/invalid)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _expected_mcp_servers() -> dict[str, dict]:
    """Spec for the SK* MCP servers an agent expects, with derived env.

    Agent name and home are derived from the environment so the spec stays
    portable across agents/machines (no hardcoded identity).
    """
    from . import DEFAULT_AGENT

    agent = (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or DEFAULT_AGENT
    )
    sk_home = os.environ.get("SKCAPSTONE_HOME") or str(Path("~/.skcapstone").expanduser())
    return {
        "skmemory": {
            "binary": "skmemory-mcp",
            "env": {"SKAGENT": agent, "SKMEMORY_AGENT": agent, "SKCAPSTONE_HOME": sk_home},
            "autofix": True,
        },
        "skcapstone": {
            "binary": "skcapstone-mcp",
            "env": {"SKAGENT": agent, "SKCAPSTONE_AGENT": agent, "SKCAPSTONE_HOME": sk_home},
            "autofix": True,
        },
        # skchat identity (SKCHAT_IDENTITY) is account-specific — never guessed.
        "skchat": {"binary": "skchat-mcp", "env": {}, "autofix": False},
    }


def _registered_mcp_servers() -> set[str]:
    """MCP server names from the locations Claude Code actually reads.

    Reads global + per-project ``mcpServers`` in ``~/.claude.json`` and a
    checked-in ``.mcp.json`` in the current directory.
    """
    names: set[str] = set()
    cc = _load_json_safe(Path("~/.claude.json").expanduser())
    names.update((cc.get("mcpServers") or {}).keys())
    for proj in (cc.get("projects") or {}).values():
        if isinstance(proj, dict):
            names.update((proj.get("mcpServers") or {}).keys())
    dotmcp = _load_json_safe(Path(".mcp.json").resolve())
    names.update((dotmcp.get("mcpServers") or {}).keys())
    return names


def _dead_config_mcp_servers() -> set[str]:
    """MCP server names defined ONLY where Claude Code does NOT read them.

    Namely ``~/.claude/settings.json``'s ``mcpServers`` block and a
    top-level ``~/.claude/mcp.json`` — both silently ignored by Claude Code.
    """
    ch = _claude_config_home()
    names: set[str] = set()
    names.update((_load_json_safe(ch / "settings.json").get("mcpServers") or {}).keys())
    names.update(_load_json_safe(ch / "mcp.json").keys())
    return names


def _is_skcapstone_binary_cmd(command: str) -> bool:
    """True only when a hook command's *executable* is the skcapstone binary.

    The SessionStart-hook check must not match on a bare ``"skcapstone" in
    command`` substring: that false-positives on a hook script living under a
    ``skcapstone-repos/`` path (e.g. skmemory's ``sk-activity-inject.sh``) and
    on the sibling ``skcapstone-mcp`` binary — neither of which is the
    ``skcapstone`` CLI. Matching the first token's basename is precise.

    Args:
        command: The full hook command string.

    Returns:
        True iff the command's first whitespace-delimited token is the
        ``skcapstone`` executable (by basename).
    """
    parts = command.strip().split()
    if not parts:
        return False
    return Path(parts[0]).name == "skcapstone"


def _check_harness_env(home: Path) -> list[Check]:
    """Validate the AI-harness (Claude Code) environment configuration.

    Catches the silent traps that leave an agent waking up cold:
      * MCP servers defined where Claude Code never reads them,
      * a SessionStart hook pointing at a stale/missing ``skcapstone`` binary,
      * a missing ``skwhisper`` CLI shim when the whisper layer is in use.

    No-ops gracefully (single informational check) when Claude Code is not
    detected, so non-Claude-Code users are not spammed with failures.

    Args:
        home: Agent home directory.

    Returns:
        List of Check results in the ``harness`` category.
    """
    checks: list[Check] = []

    if not Path("~/.claude.json").expanduser().exists():
        checks.append(Check(
            name="harness:claude-code",
            description="Claude Code config (~/.claude.json)",
            passed=True,
            detail="not detected — skipping harness checks",
            category="harness",
        ))
        return checks

    registered = _registered_mcp_servers()
    dead = _dead_config_mcp_servers()

    for name, spec in _expected_mcp_servers().items():
        if name in registered:
            checks.append(Check(
                name=f"harness:mcp:{name}",
                description=f"MCP server '{name}' registered with Claude Code",
                passed=True,
                detail="present in a config Claude Code reads",
                category="harness",
            ))
            continue

        if name in dead:
            detail = "defined ONLY in settings.json/mcp.json (not read by Claude Code)"
        else:
            detail = "not registered"
        binary = shutil.which(spec["binary"]) or spec["binary"]
        if spec["autofix"]:
            env_flags = " ".join(f"-e {k}={v}" for k, v in spec["env"].items())
            fix = f"claude mcp add {name} --scope user {env_flags} -- {binary}"
        else:
            fix = (
                f"claude mcp add {name} --scope user -e SKCHAT_IDENTITY=<your-identity> "
                f"-- {binary}  # identity is account-specific"
            )
        checks.append(Check(
            name=f"harness:mcp:{name}",
            description=f"MCP server '{name}' registered with Claude Code",
            passed=False,
            detail=detail,
            fix=fix,
            category="harness",
        ))

    # SessionStart hook must reference an existing skcapstone binary.
    settings = _load_json_safe(_claude_config_home() / "settings.json")
    hook_cmds = [
        h.get("command", "")
        for entry in (settings.get("hooks", {}).get("SessionStart") or [])
        for h in (entry.get("hooks") or [])
        if _is_skcapstone_binary_cmd(h.get("command", ""))
    ]
    if hook_cmds:
        live = shutil.which("skcapstone")
        live_real = str(Path(live).resolve()) if live else None
        missing = None
        stale = None
        hook_binary = ""
        for cmd in hook_cmds:
            hook_binary = cmd.strip().split()[0] if cmd.strip() else ""
            if "/" in hook_binary:
                resolved = Path(hook_binary).expanduser()
                if not resolved.exists():
                    missing = hook_binary
                    break
                # Present but pointing at a *different* skcapstone than PATH —
                # this is the stale-install trap (e.g. an old pyenv shim).
                if live_real and str(resolved.resolve()) != live_real:
                    stale = hook_binary
            elif not shutil.which(hook_binary):
                missing = hook_binary
                break

        if missing:
            checks.append(Check(
                name="harness:hook:sessionstart",
                description="SessionStart hook skcapstone binary",
                passed=False,
                detail=f"hook references missing binary: {missing}",
                fix=f"Repoint the hook at {live or 'the live skcapstone'} (skcapstone doctor --fix)",
                category="harness",
            ))
        elif stale:
            checks.append(Check(
                name="harness:hook:sessionstart",
                description="SessionStart hook skcapstone binary",
                passed=False,
                detail=f"hook uses {stale}, but PATH skcapstone is {live} (possible stale install)",
                fix=f"Repoint the hook at {live} (skcapstone doctor --fix)",
                category="harness",
            ))
        else:
            checks.append(Check(
                name="harness:hook:sessionstart",
                description="SessionStart hook skcapstone binary",
                passed=True,
                detail=hook_binary,
                category="harness",
            ))

    checks.extend(_check_yolo())

    # skwhisper CLI shim — only required when this agent uses the whisper layer.
    if (home / "skwhisper").exists():
        wpath = shutil.which("skwhisper")
        checks.append(Check(
            name="harness:skwhisper",
            description="skwhisper CLI on PATH",
            passed=bool(wpath),
            detail=wpath or "not found (whisper layer present but no CLI shim)",
            fix=(
                ""
                if wpath
                else "Add a shim on PATH that runs `python -m skwhisper` "
                "with the skwhisper repo on PYTHONPATH"
            ),
            category="harness",
        ))

    return checks


def _check_yolo() -> list[Check]:
    """Report the permission-bypass (YOLO) wiring for the AI harness wrappers.

    The picker's ``claude``/``codex``/``opencode`` wrapper functions append a
    permission-bypass flag only when the matching ``SK_*_YOLO`` env var is ``1``
    (see ``sk-agent-picker.sh``). This check surfaces two things that silently
    diverge otherwise:

      * whether the bypass is active in *this* environment, and
      * whether it is persisted in a shell rc file so future shells match.

    It is intentionally non-judgemental about ON vs OFF — both are valid
    depending on the box — and only flags an *inconsistency* (active in the
    current env but not persisted, so the next fresh shell would behave
    differently). Detection is best-effort: ``doctor`` runs as a subprocess and
    cannot see live shell functions, so it reads the env var and greps rc files.

    Returns:
        One Check per harness tool (claude/codex/opencode) that has YOLO active
        in the env or persisted in an rc file; nothing for tools left at the
        safe default, plus a single summary line when all are default-off.
    """
    rc_files = [
        Path.home() / ".bashrc",
        Path.home() / ".zshrc",
        Path.home() / ".bash_profile",
        Path.home() / ".profile",
    ]
    rc_text = ""
    for rc in rc_files:
        try:
            rc_text += rc.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

    tools = [
        ("claude", "SK_CLAUDE_YOLO", "--dangerously-skip-permissions"),
        ("codex", "SK_CODEX_YOLO", "--dangerously-bypass-approvals-and-sandbox"),
        ("opencode", "SK_OPENCODE_YOLO", "all-tools-allowed"),
    ]

    checks: list[Check] = []
    any_active = False
    for tool, var, flag in tools:
        env_on = os.environ.get(var, "0") == "1"
        persisted = f"export {var}=1" in rc_text or f"{var}=1" in rc_text
        if not env_on and not persisted:
            continue
        any_active = True
        if env_on and persisted:
            checks.append(Check(
                name=f"harness:yolo:{tool}",
                description=f"{tool} permission bypass ({var})",
                passed=True,
                detail=f"ENABLED globally — adds {flag}",
                category="harness",
            ))
        elif env_on and not persisted:
            checks.append(Check(
                name=f"harness:yolo:{tool}",
                description=f"{tool} permission bypass ({var})",
                passed=False,
                detail="active in this shell but NOT persisted in any rc file",
                fix=f"Add `export {var}=1` to ~/.bashrc to make it permanent",
                category="harness",
            ))
        else:  # persisted but not in current env (stale shell / rc not sourced)
            checks.append(Check(
                name=f"harness:yolo:{tool}",
                description=f"{tool} permission bypass ({var})",
                passed=True,
                detail="persisted in rc file (re-source the shell to activate)",
                category="harness",
            ))

    if not any_active:
        checks.append(Check(
            name="harness:yolo",
            description="AI-harness permission bypass (SK_*_YOLO)",
            passed=True,
            detail="disabled — wrappers run with permission prompts (safe default)",
            category="harness",
        ))

    return checks


@dataclass
class FixResult:
    """Result of attempting to auto-fix a failing check.

    Attributes:
        check_name: Name of the check that was fixed.
        success: Whether the fix succeeded.
        action: Description of what was done.
        error: Error message if the fix failed.
    """

    check_name: str
    success: bool
    action: str = ""
    error: str = ""


def run_fixes(report: DiagnosticReport, home: Path) -> list[FixResult]:
    """Attempt to auto-fix failing checks by creating missing directories and files.

    Args:
        report: Diagnostic report with failing checks.
        home: Agent home directory.

    Returns:
        List of FixResult for each attempted fix.
    """
    results: list[FixResult] = []

    for check in report.checks:
        if check.passed:
            continue

        # Fix missing directories
        if check.name == "home:exists":
            try:
                home.mkdir(parents=True, exist_ok=True)
                results.append(FixResult(
                    check_name=check.name,
                    success=True,
                    action=f"Created agent home directory {home}",
                ))
            except OSError as exc:
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error=str(exc),
                ))

        # Fix missing directories
        elif check.name.startswith("home:") and check.name != "home:manifest":
            dirname = check.name.split(":", 1)[1]
            dirpath = home / dirname
            try:
                dirpath.mkdir(parents=True, exist_ok=True)
                results.append(FixResult(
                    check_name=check.name,
                    success=True,
                    action=f"Created directory {dirpath}",
                ))
            except OSError as exc:
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error=str(exc),
                ))

        # Fix missing manifest
        elif check.name == "home:manifest":
            manifest_path = home / "manifest.json"
            try:
                if manifest_path.exists():
                    raise FileExistsError(f"Refusing to overwrite existing manifest: {manifest_path}")
                data = {
                    "name": os.environ.get("SKCAPSTONE_AGENT", "sovereign"),
                    "version": "0.0.0",
                    "created_at": "",
                    "connectors": [],
                }
                manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                results.append(FixResult(
                    check_name=check.name,
                    success=True,
                    action=f"Created default manifest at {manifest_path}",
                ))
            except (OSError, FileExistsError) as exc:
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error=str(exc),
                ))

        # Fix missing memory store
        elif check.name == "memory:store":
            memory_dir = _resolve_memory_dir(home)
            try:
                for layer in ("short-term", "mid-term", "long-term"):
                    (memory_dir / layer).mkdir(parents=True, exist_ok=True)
                results.append(FixResult(
                    check_name=check.name,
                    success=True,
                    action=f"Created memory directories at {memory_dir}",
                ))
            except OSError as exc:
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error=str(exc),
                ))

        # Rebuild missing memory index
        elif check.name == "memory:index":
            memory_dir = _resolve_memory_dir(home)
            index_path = memory_dir / "index.json"
            try:
                index_data: dict[str, dict] = {}
                for layer in ("short-term", "mid-term", "long-term"):
                    layer_dir = memory_dir / layer
                    if not layer_dir.exists():
                        continue
                    for memory_file in layer_dir.glob("*.json"):
                        try:
                            payload = json.loads(memory_file.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            continue
                        memory_id = payload.get("memory_id") or payload.get("id") or memory_file.stem
                        index_data[memory_id] = {
                            "layer": layer,
                            "tags": payload.get("tags", []),
                            "importance": payload.get("importance"),
                            "created_at": payload.get("created_at"),
                        }
                memory_dir.mkdir(parents=True, exist_ok=True)
                index_path.write_text(json.dumps(index_data, indent=2), encoding="utf-8")
                results.append(FixResult(
                    check_name=check.name,
                    success=True,
                    action=f"Rebuilt memory index at {index_path}",
                ))
            except OSError as exc:
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error=str(exc),
                ))

        # Fix missing sync directory
        elif check.name == "sync:dir":
            sync_dir = home / "sync"
            try:
                for subdir in ("outbox", "inbox", "archive"):
                    (sync_dir / subdir).mkdir(parents=True, exist_ok=True)
                results.append(FixResult(
                    check_name=check.name,
                    success=True,
                    action=f"Created sync directories at {sync_dir}",
                ))
            except OSError as exc:
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error=str(exc),
                ))

        # Fix Codex global SK agent context bootstrap
        elif check.name == "codex:agent_context":
            try:
                from .codex_setup import ensure_codex_setup

                actions = ensure_codex_setup()
                results.append(FixResult(
                    check_name=check.name,
                    success=True,
                    action=", ".join(actions) if actions else "Codex bootstrap already configured",
                ))
            except OSError as exc:
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error=str(exc),
                ))

        # Register a missing MCP server with Claude Code (user scope).
        elif check.name.startswith("harness:mcp:"):
            name = check.name.split(":", 2)[2]
            spec = _expected_mcp_servers().get(name)
            if not spec or not spec.get("autofix"):
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error="manual fix required (identity is account-specific) — see hint",
                ))
            elif not shutil.which("claude"):
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error="claude CLI not found on PATH",
                ))
            else:
                binary = shutil.which(spec["binary"]) or spec["binary"]
                cmd = ["claude", "mcp", "add", name, "--scope", "user"]
                for key, val in spec["env"].items():
                    cmd += ["-e", f"{key}={val}"]
                cmd += ["--", binary]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    if proc.returncode == 0:
                        results.append(FixResult(
                            check_name=check.name,
                            success=True,
                            action=f"Registered MCP server '{name}' (user scope)",
                        ))
                    else:
                        results.append(FixResult(
                            check_name=check.name,
                            success=False,
                            error=(proc.stderr or proc.stdout).strip()[:200],
                        ))
                except (subprocess.SubprocessError, OSError) as exc:
                    results.append(FixResult(
                        check_name=check.name,
                        success=False,
                        error=str(exc),
                    ))

        # Repoint a stale SessionStart hook at the live skcapstone binary.
        elif check.name == "harness:hook:sessionstart":
            live = shutil.which("skcapstone")
            settings_path = _claude_config_home() / "settings.json"
            if not live:
                results.append(FixResult(
                    check_name=check.name,
                    success=False,
                    error="live skcapstone not found on PATH",
                ))
            else:
                try:
                    data = json.loads(settings_path.read_text(encoding="utf-8"))
                    changed = False
                    for entry in data.get("hooks", {}).get("SessionStart", []):
                        for hook in entry.get("hooks", []):
                            cmd_str = hook.get("command", "")
                            # Match the executable, not a substring — otherwise a
                            # hook script under skcapstone-repos/ would have its
                            # path destructively rewritten to the skcapstone binary.
                            if not _is_skcapstone_binary_cmd(cmd_str):
                                continue
                            parts = cmd_str.split()
                            if parts and parts[0] != live:
                                parts[0] = live
                                hook["command"] = " ".join(parts)
                                changed = True
                    if changed:
                        settings_path.write_text(
                            json.dumps(data, indent=2) + "\n", encoding="utf-8"
                        )
                        results.append(FixResult(
                            check_name=check.name,
                            success=True,
                            action=f"Repointed SessionStart hook to {live}",
                        ))
                    else:
                        results.append(FixResult(
                            check_name=check.name,
                            success=False,
                            error="no stale skcapstone hook command found to repair",
                        ))
                except (OSError, json.JSONDecodeError) as exc:
                    results.append(FixResult(
                        check_name=check.name,
                        success=False,
                        error=str(exc),
                    ))

    return results


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
