"""Deploy drift: anything that makes production something other than released main.

One function computes the findings; ``doctor`` renders them as checks and the
drift timer turns a CHANGE in them into one alert. An unchanged finding set
sends nothing, so a known, accepted state never becomes noise.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .inspect import default_unit_dir, exec_paths_in_trees, list_dists, normalize
from .manifest import Manifest


@dataclass
class DriftReport:
    """Findings, grouped by kind. Each entry is one stable, sortable line."""

    editable: list[str] = field(default_factory=list)
    exec_in_tree: list[str] = field(default_factory=list)
    behind: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        """Every finding as one line, prefixed with its kind."""
        return (
            [f"editable: {x}" for x in self.editable]
            + [f"exec-in-tree: {x}" for x in self.exec_in_tree]
            + [f"version: {x}" for x in self.behind]
        )

    def digest(self) -> str:
        """Stable fingerprint of the findings, for change detection."""
        return hashlib.sha256("\n".join(sorted(self.lines())).encode()).hexdigest()[:16]


def compute(
    home: Path,
    venv_python: Path,
    manifest: Optional[Manifest] = None,
    unit_dir: Optional[Path] = None,
    node: Optional[str] = None,
) -> DriftReport:
    """Compute the drift findings for this node.

    Args:
        home: The user's home directory.
        venv_python: The deploy venv's interpreter (``~/.skenv/bin/python``).
        manifest: The release set, when available; without it the version
            comparison is reported as unknown rather than as a pass.
        unit_dir: systemd user unit directory override.
        node: Node profile override.

    Returns:
        The drift report.
    """
    report = DriftReport()
    try:
        dists = list_dists(venv_python)
    except (RuntimeError, OSError, ValueError) as exc:
        report.unknown.append(f"cannot list {venv_python}: {exc}")
        dists = []
    for dist in dists:
        if dist.editable:
            report.editable.append(
                f"{dist.name} {dist.version} -> {dist.location.removeprefix('file://')}"
            )
    allow = manifest.allow_exec_in_tree if manifest else []
    for ep, tree in exec_paths_in_trees(home, unit_dir or default_unit_dir(home), allow):
        report.exec_in_tree.append(f"{ep.origin}: {ep.path} (tree {tree})")
    if manifest is None:
        report.unknown.append("no release manifest on this node; versions not compared")
    elif dists:
        have = {normalize(d.name): d for d in dists}
        for name, spec in manifest.packages_for(node).items():
            dist = have.get(normalize(name))
            if dist is None:
                report.behind.append(f"{name} not installed (manifest {spec.version or spec.ref})")
            elif spec.source == "pypi" and dist.version != spec.version:
                report.behind.append(f"{name} {dist.version} != manifest {spec.version}")
    return report


def alert_on_change(report: DriftReport, state_file: Path, sender: Optional[str] = None) -> str:
    """Send one alert when the finding set changed since the last run.

    New or changed findings send a warning into the ``deploy/drift/<host>``
    thread; findings that all cleared send the resolving last word. Nothing
    changed means nothing is sent.

    Args:
        report: This run's findings.
        state_file: Where the last digest is kept (host-local).
        sender: The ``sk-alert`` executable (found on PATH by default).

    Returns:
        ``sent``, ``resolved``, ``unchanged`` or ``no-sender``.
    """
    try:
        last = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    digest = report.digest() if report.lines() else ""
    if digest == last.get("digest", ""):
        return "unchanged"
    sender = sender or shutil.which("sk-alert")
    host = socket.gethostname()
    group = f"deploy/drift/{host}"
    if sender is None:
        return "no-sender"
    if digest:
        lines = report.lines()
        body = (
            f"{host}: {len(lines)} deploy drift finding(s), production is not released main.\n"
            + "\n".join(lines[:25])
        )
        if len(lines) > 25:
            body += f"\n... and {len(lines) - 25} more (skcapstone deploy drift)"
        cmd = [sender, "-l", "warn", "-g", group, "--title", f"deploy drift on {host}", body]
        outcome = "sent"
    else:
        cmd = [sender, "-g", group, "--resolve", f"{host}: deploy drift cleared"]
        outcome = "resolved"
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        return f"send-failed: {proc.stderr.strip()[-200:]}"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"digest": digest}), encoding="utf-8")
    return outcome
