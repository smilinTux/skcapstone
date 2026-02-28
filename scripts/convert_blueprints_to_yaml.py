#!/usr/bin/env python3
"""Convert soul blueprint markdown files to structured YAML.

Reads all .md blueprints from the souls-blueprints repo and writes
corresponding .yaml files using the existing soul.py parser.

Usage:
    python scripts/convert_blueprints_to_yaml.py [--source DIR] [--dest DIR]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

# Add src to path so we can import soul.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from skcapstone.soul import parse_blueprint, _slugify


def _slug_from_filename(path: Path) -> str:
    """Derive a clean slug from the filename, not the display name."""
    stem = path.stem.lower()
    # Remove leading 'the-' or 'the_' for cleaner slugs
    stem = re.sub(r"[^\w\s-]", "", stem)
    stem = re.sub(r"[\s_]+", "-", stem)
    return stem.strip("-")


def convert_one(md_path: Path, dest_dir: Path) -> Path:
    """Convert a single MD blueprint to YAML.

    Args:
        md_path: Path to the .md blueprint file.
        dest_dir: Directory to write the .yaml file.

    Returns:
        Path to the written YAML file.
    """
    bp = parse_blueprint(md_path)

    # Use filename-based slug for predictable, short names
    slug = _slug_from_filename(md_path)

    # Build the YAML structure
    data = {
        "name": slug,
        "display_name": bp.display_name,
        "category": bp.category,
        "vibe": bp.vibe or "",
        "philosophy": bp.philosophy or "",
        "emoji": bp.emoji,
        "core_traits": bp.core_traits if bp.core_traits else [],
        "communication_style": {
            "patterns": bp.communication_style.patterns,
            "tone_markers": bp.communication_style.tone_markers,
            "signature_phrases": bp.communication_style.signature_phrases,
        },
        "decision_framework": bp.decision_framework,
        "emotional_topology": bp.emotional_topology if bp.emotional_topology else {},
    }

    # Clean up None values in nested dicts
    if not data["communication_style"]["patterns"]:
        del data["communication_style"]["patterns"]
    if not data["communication_style"]["tone_markers"]:
        del data["communication_style"]["tone_markers"]
    if not data["communication_style"]["signature_phrases"]:
        del data["communication_style"]["signature_phrases"]
    if not data["communication_style"]:
        data["communication_style"] = {}

    dest_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = dest_dir / f"{slug}.yaml"

    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(
            data,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )

    return yaml_path


def convert_all(source: Path, dest: Path) -> tuple[int, int]:
    """Convert all MD blueprints preserving category directories.

    Args:
        source: Root directory containing category subdirs with .md files.
        dest: Root directory for YAML output.

    Returns:
        Tuple of (success_count, failure_count).
    """
    successes = 0
    failures = 0

    for md_path in sorted(source.rglob("*.md")):
        if md_path.name.startswith(".") or md_path.name.upper() == "README.MD":
            continue

        # Preserve category directory structure
        rel = md_path.parent.relative_to(source)
        dest_dir = dest / rel

        try:
            yaml_path = convert_one(md_path, dest_dir)
            print(f"  {md_path.name} -> {yaml_path.relative_to(dest)}")
            successes += 1
        except Exception as exc:
            print(f"  FAIL: {md_path.name}: {exc}", file=sys.stderr)
            failures += 1

    return successes, failures


def main():
    parser = argparse.ArgumentParser(description="Convert soul blueprints MD -> YAML")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent
        / "souls-blueprints"
        / "blueprints",
        help="Source directory with MD blueprints",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent
        / "souls-blueprints"
        / "yaml",
        help="Destination directory for YAML output",
    )
    args = parser.parse_args()

    print(f"Source: {args.source}")
    print(f"Dest:   {args.dest}")
    print()

    ok, fail = convert_all(args.source, args.dest)
    print(f"\nConverted: {ok}, Failed: {fail}")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
