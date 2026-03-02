#!/usr/bin/env python3
"""Version bumping script for skcapstone and companion packages.

Usage:
    python scripts/bump_version.py patch          # 0.1.0 → 0.1.1
    python scripts/bump_version.py minor          # 0.1.0 → 0.2.0
    python scripts/bump_version.py major          # 0.1.0 → 1.0.0
    python scripts/bump_version.py patch --dry-run
    python scripts/bump_version.py 0.2.3           # set explicit version

Options:
    --pkg PATH    Path to a specific package dir (default: current dir)
    --tag         Create and push a git tag after bumping
    --push        Push the commit after bumping
    --dry-run     Print what would happen without making changes
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path


VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def parse_version(v: str) -> tuple[int, int, int]:
    parts = v.split(".")
    if len(parts) != 3:
        raise ValueError(f"Version must be X.Y.Z, got: {v!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def bump(version: str, part: str) -> str:
    major, minor, patch = parse_version(version)
    if part == "major":
        return f"{major + 1}.0.0"
    elif part == "minor":
        return f"{major}.{minor + 1}.0"
    elif part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        # treat as explicit version
        parse_version(part)  # validate
        return part


def read_version(pyproject: Path) -> str:
    text = pyproject.read_text()
    m = VERSION_RE.search(text)
    if not m:
        raise RuntimeError(f"Cannot find version in {pyproject}")
    return m.group(1)


def write_version(pyproject: Path, old: str, new: str, dry_run: bool) -> None:
    text = pyproject.read_text()
    new_text = VERSION_RE.sub(f'version = "{new}"', text, count=1)
    if dry_run:
        print(f"  [dry-run] Would write version {old} → {new} in {pyproject}")
    else:
        pyproject.write_text(new_text)
        print(f"  Updated {pyproject}: {old} → {new}")


def run(cmd: list[str], dry_run: bool, cwd: Path | None = None) -> None:
    print(f"  $ {' '.join(cmd)}")
    if not dry_run:
        subprocess.run(cmd, check=True, cwd=cwd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bump package version")
    parser.add_argument(
        "part",
        help="Bump part: major | minor | patch | X.Y.Z (explicit)",
    )
    parser.add_argument(
        "--pkg",
        type=Path,
        default=Path("."),
        help="Path to package directory containing pyproject.toml",
    )
    parser.add_argument(
        "--tag",
        action="store_true",
        help="Create a git tag v<new_version> after bumping",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push commit (and tag if --tag) to origin",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making changes",
    )
    args = parser.parse_args()

    pyproject = args.pkg / "pyproject.toml"
    if not pyproject.exists():
        print(f"ERROR: {pyproject} not found", file=sys.stderr)
        sys.exit(1)

    old_version = read_version(pyproject)
    new_version = bump(old_version, args.part)

    print(f"Package: {args.pkg.resolve().name}")
    print(f"Version: {old_version} → {new_version}")

    write_version(pyproject, old_version, new_version, args.dry_run)

    # Git commit
    run(["git", "add", str(pyproject)], args.dry_run, cwd=args.pkg)
    run(
        ["git", "commit", "-m", f"chore: bump version to {new_version}"],
        args.dry_run,
        cwd=args.pkg,
    )

    if args.tag:
        tag = f"v{new_version}"
        run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], args.dry_run, cwd=args.pkg)
        print(f"  Tagged: {tag}")

    if args.push:
        run(["git", "push"], args.dry_run, cwd=args.pkg)
        if args.tag:
            run(["git", "push", "--tags"], args.dry_run, cwd=args.pkg)

    if args.dry_run:
        print("\n[dry-run complete — no changes were made]")
    else:
        print(f"\nDone. To publish: git push && git push --tags")
        print(f"  (or use --push --tag flags next time)")


if __name__ == "__main__":
    main()
