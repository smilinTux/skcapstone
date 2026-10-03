"""Discovery and audit engine for the Syncthing private-material policy.

The audit answers, for every configured Syncthing folder on this host: can
this folder synchronize private keys, revocation certificates, passphrases,
secret keyrings, or token stores? It answers at the ACTUAL folder root:
the .stignore read is the one Syncthing would read there, never the
~/.skcapstone copy assumed to apply elsewhere. Every uncertain answer
(missing rules, unreadable files, unsupported pattern syntax, truncated
scan) fails closed and counts as uncovered.

Nothing here writes unless ``apply=True`` is passed explicitly, and even
then the only write is an additive union-merge into a folder root's
.stignore with a backup, reusing the installer merge so this path can never
remove a rule either.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..skills.syncthing_setup import _merge_stignore
from .discovery import DiscoveredFolder, discover_folders
from .model import MATERIAL_CLASSES, Finding, FolderReport, MaterialClass, SyncPolicyReport
from .stignore import CompiledPattern, Coverage, compile_pattern, evaluate, load_ruleset

#: Bound on walk work per folder; real nodes stay far below it because the
#: walk prunes directories the folder's own rules ignore.
MAX_WALK_ENTRIES = 200_000

#: Private key files whose PGP fingerprints feed duplicate detection.
FINGERPRINT_FILE_NAMES = frozenset({"private.asc", "agent.key"})

#: Suffixes that mark private material inside a legacy capauth root.
_LEGACY_MATERIAL_SUFFIXES = frozenset({".asc", ".key", ".pem"})

_SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


def _walk_files(root: Path, rules: list[CompiledPattern]) -> tuple[list[str], bool]:
    """List relative file paths under a folder root, pruning ignored dirs.

    A directory the folder's own rules ignore is never descended into:
    Syncthing cannot announce what it never scans, so nothing beneath an
    ignored directory can leak through this folder. Uncertain evaluations
    are NOT pruned, which keeps the walk fail closed.

    Args:
        root: Resolved folder root.
        rules: Compiled ignore rules from the root's .stignore.

    Returns:
        Relative file paths, and whether the entry cap truncated the walk.
    """
    files: list[str] = []
    truncated = False
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        kept = []
        for name in sorted(dirnames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if evaluate(rules, rel, is_dir=True) is not Coverage.IGNORED:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            files.append(rel)
        visited += len(kept) + len(filenames)
        if visited > MAX_WALK_ENTRIES:
            truncated = True
            break
    return files, truncated


def _matches_material(
    relpath: str,
    matchers: list[tuple[str, list[CompiledPattern]]],
) -> str | None:
    """The name of the first material class whose globs match a path.

    Args:
        relpath: Slash-separated path relative to the folder root.
        matchers: (class name, compiled glob patterns) pairs.

    Returns:
        The class name, or None when nothing matches.
    """
    for name, patterns in matchers:
        for rule in patterns:
            if rule.supported and rule.regex is not None and rule.regex.match(relpath):
                return name
    return None


def _material_matchers() -> list[tuple[str, list[CompiledPattern]]]:
    """Compiled detection globs for every material class, in report order."""
    matchers: list[tuple[str, list[CompiledPattern]]] = []
    for material in MATERIAL_CLASSES:
        compiled = [compile_pattern(glob) for glob in material.globs]
        matchers.append((material.name, [rule for rule in compiled if rule is not None]))
    return matchers


def _class_by_name(name: str) -> MaterialClass:
    """The material class record for a name (always present by construction).

    Args:
        name: Material class name from a detection match.

    Returns:
        The matching MaterialClass.

    Raises:
        StopIteration: If the name is unknown; unreachable from audit flow.
    """
    return next(material for material in MATERIAL_CLASSES if material.name == name)


def audit_folder(folder: DiscoveredFolder) -> tuple[FolderReport, list[Path]]:
    """Audit one folder root for private-material exposure.

    Args:
        folder: The discovered folder with its resolved root.

    Returns:
        The folder report and the private key files found under the root
        (for duplicate fingerprint detection). A root absent from this host
        reports info-only and cannot leak: a folder a node does not hold
        cannot announce anything through it.
    """
    root = folder.path
    if not root.is_dir():
        return (
            FolderReport(
                folder_id=folder.folder_id,
                path=str(root),
                present_on_host=False,
                stignore_present=False,
                findings=(
                    Finding(
                        severity="info",
                        category="folder_not_held",
                        path=str(root),
                        detail="folder root is not present on this host",
                    ),
                ),
            ),
            [],
        )

    findings: list[Finding] = []
    stignore_path = root / ".stignore"
    try:
        rules = load_ruleset(stignore_path.read_text(encoding="utf-8"))
        stignore_present = True
    except OSError:
        rules = []
        stignore_present = False
        findings.append(
            Finding(
                severity="error",
                category="stignore_missing",
                path=str(stignore_path),
                detail="folder root has no readable .stignore; nothing is ignored",
            )
        )

    for material in MATERIAL_CLASSES:
        for probe, line in material.probes:
            if evaluate(rules, probe) is not Coverage.IGNORED:
                findings.append(
                    Finding(
                        severity="error",
                        category="private_pattern_uncovered",
                        path=probe,
                        detail=f"{material.name} material landing here would synchronize",
                        remediation=(line,),
                    )
                )

    files, truncated = _walk_files(root, rules)
    if truncated:
        findings.append(
            Finding(
                severity="warn",
                category="scan_truncated",
                path=str(root),
                detail="folder walk hit the entry cap; coverage is not fully proven",
            )
        )

    matchers = _material_matchers()
    key_files: list[Path] = []
    for relpath in files:
        material_name = _matches_material(relpath, matchers)
        if material_name is None:
            continue
        if Path(relpath).name in FINGERPRINT_FILE_NAMES:
            key_files.append(root / relpath)
        if evaluate(rules, relpath) is Coverage.IGNORED:
            findings.append(
                Finding(
                    severity="warn",
                    category="private_material_present_covered",
                    path=relpath,
                    detail=f"{material_name} material is present but currently ignored",
                )
            )
        else:
            remediation = tuple(line for _, line in _class_by_name(material_name).probes)
            findings.append(
                Finding(
                    severity="error",
                    category="private_material_uncovered",
                    path=relpath,
                    detail=f"{material_name} material exists and can synchronize",
                    remediation=remediation,
                )
            )

    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 3), f.category, f.path))
    return (
        FolderReport(
            folder_id=folder.folder_id,
            path=str(root),
            present_on_host=True,
            stignore_present=stignore_present,
            findings=tuple(findings),
        ),
        key_files,
    )


def _fingerprint_of(path: Path) -> str | None:
    """The PGP fingerprint of a key file, or None when it cannot be parsed.

    Uses PGPy lazily, the same library fleet.signing uses, so the audit runs
    on hosts without it; unavailability is reported by the caller as its own
    finding rather than silently skipping duplicate detection.

    Args:
        path: A private key file (private.asc or agent.key).

    Returns:
        The fingerprint without spaces, or None on any parse failure.
    """
    import pgpy  # type: ignore[import-untyped]

    try:
        key, _ = pgpy.PGPKey.from_file(str(path))
    except Exception:  # noqa: BLE001 - any parse failure means no fingerprint
        return None
    return str(key.fingerprint).replace(" ", "")


def _fingerprint_findings(key_files: list[Path]) -> list[Finding]:
    """Report PGP fingerprints found in more than one location.

    Args:
        key_files: Every private.asc/agent.key discovered under synced
            folder roots, agent homes, and the legacy root.

    Returns:
        An error finding per duplicated fingerprint, or a warn finding when
        PGPy is unavailable and duplicates cannot be ruled out.
    """
    try:
        import pgpy  # noqa: F401  # type: ignore[import-untyped]
    except ImportError:
        return [
            Finding(
                severity="warn",
                category="fingerprint_check_unavailable",
                detail="pgpy is not installed; duplicate fingerprints cannot be ruled out",
            )
        ]
    by_fingerprint: dict[str, list[str]] = {}
    for path in key_files:
        fingerprint = _fingerprint_of(path)
        if fingerprint is None:
            continue
        if str(path) not in by_fingerprint.setdefault(fingerprint, []):
            by_fingerprint[fingerprint].append(str(path))
    findings: list[Finding] = []
    for fingerprint in sorted(by_fingerprint):
        paths = sorted(by_fingerprint[fingerprint])
        if len(paths) > 1:
            findings.append(
                Finding(
                    severity="error",
                    category="duplicate_private_fingerprint",
                    path=paths[0],
                    detail=(
                        f"private key fingerprint {fingerprint} appears in "
                        f"{len(paths)} locations: {', '.join(paths)}"
                    ),
                )
            )
    return findings


def _legacy_root_findings(legacy_home: Path, synced_roots: list[Path]) -> list[Finding]:
    """Report a legacy ~/.capauth root holding private material.

    Args:
        legacy_home: The legacy capauth home to inspect.
        synced_roots: Resolved roots of every discovered synced folder.

    Returns:
        An error finding when private material under the legacy root sits
        inside a synced folder (or contains one), a warn finding when it is
        present but unsynced, and nothing when the root is absent or clean.
    """
    if not legacy_home.is_dir():
        return []
    material: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(legacy_home):
        dirnames[:] = sorted(dirnames)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix in _LEGACY_MATERIAL_SUFFIXES or name.startswith("revocation"):
                material.append(path)
    if not material:
        return []
    synced = any(
        legacy_home.is_relative_to(root) or root.is_relative_to(legacy_home)
        for root in synced_roots
    )
    detail = f"legacy capauth root holds {len(material)} private material file(s)"
    if synced:
        return [
            Finding(
                severity="error",
                category="legacy_capauth_root",
                path=str(legacy_home),
                detail=detail + " inside a synchronized folder",
            )
        ]
    return [
        Finding(
            severity="warn",
            category="legacy_capauth_root",
            path=str(legacy_home),
            detail=detail + "; it is not inside any synchronized folder",
        )
    ]


def _agent_home_key_files(agent_home: Path) -> list[Path]:
    """Every agent identity private key under the shared agent home.

    Args:
        agent_home: The shared root holding agents/*/capauth/identity/.

    Returns:
        Existing private key paths; empty when the home is absent.
    """
    agents_dir = agent_home / "agents"
    if not agents_dir.is_dir():
        return []
    out: list[Path] = []
    for identity in sorted(agents_dir.glob("*/capauth/identity")):
        for name in sorted(FINGERPRINT_FILE_NAMES):
            candidate = identity / name
            if candidate.is_file():
                out.append(candidate)
    return out


def apply_remediation(reports: list[FolderReport]) -> tuple[str, ...]:
    """Union-merge remediation lines into each folder root's .stignore.

    Additive only and idempotent, reusing the installer's merge: existing
    rules are never removed, a backup is written before any change, and a
    second run changes nothing. Folders not held on this host are skipped.

    Args:
        reports: Folder reports carrying remediation lines.

    Returns:
        The .stignore paths actually modified.
    """
    applied: list[str] = []
    for report in reports:
        lines = report.remediation_lines
        if not lines or not report.present_on_host:
            continue
        stignore_path = Path(report.path) / ".stignore"
        try:
            existing = stignore_path.read_text(encoding="utf-8") if stignore_path.exists() else ""
        except OSError:
            continue
        merged = _merge_stignore(existing, "\n".join(lines) + "\n")
        if merged == existing:
            continue
        try:
            if existing:
                stignore_path.with_name(".stignore.bak-sync-policy").write_text(
                    existing, encoding="utf-8"
                )
            stignore_path.write_text(merged, encoding="utf-8")
        except OSError:
            continue
        applied.append(str(stignore_path))
    return tuple(applied)


def audit(
    *,
    home: Path | None = None,
    config_path: Path | None = None,
    agent_home: Path | None = None,
    legacy_home: Path | None = None,
    apply: bool = False,
) -> SyncPolicyReport:
    """Run the full private-material policy audit on this host.

    Args:
        home: Home directory for config discovery and default roots.
        config_path: Explicit Syncthing config.xml override.
        agent_home: Shared agent home holding agents/*/capauth/identity/.
        legacy_home: Legacy capauth root to check for private material.
        apply: When True, union-merge remediation lines into folder roots.

    Returns:
        The structured report; ``ok`` is False when anything error-grade
        was found, which is the fail-closed verdict the CLI exits on.
    """
    home = home or Path.home()
    agent_home = agent_home or home / ".skcapstone"
    legacy_home = legacy_home or home / ".capauth"

    folders, findings = discover_folders(home, config_path=config_path)

    reports: list[FolderReport] = []
    key_files: list[Path] = []
    for folder in folders:
        report, keys = audit_folder(folder)
        reports.append(report)
        key_files.extend(keys)

    synced_roots = [folder.path for folder in folders if folder.path.is_dir()]
    findings.extend(_legacy_root_findings(legacy_home, synced_roots))

    key_files.extend(_agent_home_key_files(agent_home))
    for name in sorted(FINGERPRINT_FILE_NAMES):
        candidate = legacy_home / "identity" / name
        if candidate.is_file():
            key_files.append(candidate)
    findings.extend(_fingerprint_findings(key_files))

    applied: tuple[str, ...] = ()
    if apply:
        applied = apply_remediation(reports)

    return SyncPolicyReport(
        folders=tuple(reports),
        findings=tuple(findings),
        dry_run=not apply,
        applied=applied,
    )
