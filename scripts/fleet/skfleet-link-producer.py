#!/usr/bin/env python3
"""Refresh Link lineage and publish its mediated observation feed."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPOSITORY_SCOPE_ENV = "SKFLEET_LINK_REPOSITORIES"
REQUIRED_REPOSITORIES = frozenset(
    {
        "smilinTux/skcapstone",
        "smilinTux/skdashboard",
        "smilinTux/skworld",
        "smilinTux/sk-standards",
    }
)
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def repository_scope() -> tuple[str, ...] | None:
    """Return the exact configured lifecycle repository scope."""
    raw = os.environ.get(REPOSITORY_SCOPE_ENV, "")
    parts = raw.split(",")
    repositories = tuple(part.strip() for part in parts)
    if (
        not repositories
        or any(not repository for repository in repositories)
        or len(repositories) != len(set(repositories))
        or any(REPOSITORY_PATTERN.fullmatch(repository) is None for repository in repositories)
        or set(repositories) != REQUIRED_REPOSITORIES
    ):
        return None
    return repositories


def run(
    *,
    home: Path,
) -> int:
    """Run the role-bounded producer pipeline and preserve incomplete fallback."""
    if (
        os.environ.get("SKAGENT") != "link-producer"
        or os.environ.get("SKCAPSTONE_AGENT") != "link-producer"
    ):
        print('{"healthy":false,"reason":"producer_identity_required"}')
        return 77
    repositories = repository_scope()
    if repositories is None:
        print('{"healthy":false,"reason":"repository_scope_invalid"}')
        return 78

    coordination = home / "coordination"
    lineage = coordination / "link-lineage.json"
    output = coordination / "link-observations.json"
    reconciler = Path(__file__).with_name("link-lineage.py")
    reconcile = subprocess.run(
        [
            sys.executable,
            str(reconciler),
            "--home",
            str(home),
            "--output",
            str(lineage),
        ]
        + [argument for repository in repositories for argument in ("--repo", repository)],
        check=False,
    )
    if reconcile.returncode:
        return reconcile.returncode

    produce = subprocess.run(
        [
            sys.executable,
            "-m",
            "skcapstone.link_observation_producer",
            "--lineage",
            str(lineage),
            "--output",
            str(output),
        ]
        + [argument for repository in repositories for argument in ("--repo", repository)],
        check=False,
        capture_output=True,
        text=True,
    )
    if produce.stdout:
        print(produce.stdout, end="")
    if produce.stderr:
        print(produce.stderr, end="", file=sys.stderr)
    if produce.returncode != 78:
        return produce.returncode
    try:
        result = json.loads(produce.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return produce.returncode
    return 0 if result.get("reason") == "lineage_incomplete" else produce.returncode


def main() -> int:
    """Run against the current user's SKCapstone home."""
    return run(home=Path.home() / ".skcapstone")


if __name__ == "__main__":
    raise SystemExit(main())
