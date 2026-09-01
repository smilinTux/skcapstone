#!/usr/bin/env python3
"""Sync the packaged systemd unit tree from the canonical top-level tree.

There is exactly ONE source of truth for the skcapstone systemd units:
the top-level ``systemd/`` directory. That tree is what ``scripts/install.sh``
deploys on a source checkout, and it carries the correct ``%h/.skenv/bin``
ExecStart paths plus the relaxed hardening from the unit-hardening work
(ProtectHome=read-only / ProtectSystem=strict were removed because a missing
ReadWritePaths dir makes them fail-closed and the daemon never starts).

The daemon's own installer (``skcapstone.systemd.install_service``) copies from
the *packaged* mirror ``src/skcapstone/data/systemd/`` (``BUNDLED_DIR``), because
that tree ships inside the wheel for the PyPI / cold-machine install path. Those
two trees MUST stay byte-identical or a cold machine gets non-working units.

This script regenerates the packaged mirror from the canonical tree. Run it
whenever a top-level unit changes. ``tests/test_systemd.py`` has a drift-guard
test that fails if the trees diverge, so CI catches a forgotten sync.

Usage:
    python scripts/sync-systemd-units.py            # sync
    python scripts/sync-systemd-units.py --check     # exit 1 if drift (no write)
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_DIR = REPO_ROOT / "systemd"
PACKAGED_DIR = REPO_ROOT / "src" / "skcapstone" / "data" / "systemd"

# Only these unit suffixes ship in the wheel (see pyproject package-data).
UNIT_GLOBS = ("*.conf", "*.service", "*.socket", "*.timer")


def _unit_files(directory: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for pattern in UNIT_GLOBS:
        for path in directory.glob(pattern):
            files[path.name] = path
    return files


def check() -> list[str]:
    """Return a list of human-readable drift descriptions (empty == in sync)."""
    canonical = _unit_files(CANONICAL_DIR)
    packaged = _unit_files(PACKAGED_DIR)
    problems: list[str] = []

    for name in sorted(set(canonical) - set(packaged)):
        problems.append(f"missing from packaged tree: {name}")
    for name in sorted(set(packaged) - set(canonical)):
        problems.append(f"extra in packaged tree (not in canonical): {name}")
    for name in sorted(set(canonical) & set(packaged)):
        if canonical[name].read_bytes() != packaged[name].read_bytes():
            problems.append(f"content differs: {name}")
    return problems


def sync() -> int:
    """Copy canonical units into the packaged tree. Returns count changed."""
    canonical = _unit_files(CANONICAL_DIR)
    packaged = _unit_files(PACKAGED_DIR)
    PACKAGED_DIR.mkdir(parents=True, exist_ok=True)

    changed = 0
    for name, src in canonical.items():
        dst = PACKAGED_DIR / name
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            shutil.copy2(src, dst)
            changed += 1

    # Remove packaged units that no longer exist in the canonical tree.
    for name in set(packaged) - set(canonical):
        (PACKAGED_DIR / name).unlink()
        changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit 1 without writing",
    )
    args = parser.parse_args()

    if args.check:
        problems = check()
        if problems:
            print("systemd unit trees are OUT OF SYNC:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            print(
                "\nRun: python scripts/sync-systemd-units.py",
                file=sys.stderr,
            )
            return 1
        print("systemd unit trees are in sync.")
        return 0

    changed = sync()
    if changed:
        print(f"Synced {changed} unit file(s) into {PACKAGED_DIR}.")
    else:
        print("Already in sync; nothing to do.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
