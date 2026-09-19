#!/usr/bin/env python3
"""Fold `changelog.d/*.md` fragments into `CHANGELOG.md`.

One file per PR is what makes the rebase conflict on a shared CHANGELOG.md
structurally impossible rather than merely less likely (see changelog.d/README.md).
This is the other half: the step that collects them back into one document.

    python scripts/changelog_fragments.py --check   # list pending, change nothing
    python scripts/changelog_fragments.py           # fold them in, delete them

Deliberately a ~40-line local script and not towncrier or scriv:

* Neither tool has a release moment to hook here. `publish.yml` cuts the next
  patch tag on EVERY merge to main, so "at release time" means "every merge",
  and a build step that runs every merge is just a commit step with extra config.
* Both want to own CHANGELOG.md through a template. This one is 2200+ lines of
  hand-written prose in three different heading styles, and regenerating it is a
  history rewrite, not a build.
* Both impose a `<issue>.<type>.md` naming taxonomy. The gate only needs "a new
  file appeared", and every category argument is a category argument you have to
  have on every PR forever.
* A pinned dev dependency is a real cost in a repo that pins exactly
  (black==26.5.1, ruff==0.15.4) and installs the package with extras in CI.

Exit 0 on success, 1 on a refusal.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRAGMENT_DIR = REPO / "changelog.d"
CHANGELOG = REPO / "CHANGELOG.md"
ANCHOR = "## Unreleased"
NOT_ENTRIES = {"README.md", ".gitkeep", ".gitignore"}


def fragments() -> list[Path]:
    """Every real fragment, oldest first. Mirrors the gate's definition exactly."""
    return sorted(
        p for p in FRAGMENT_DIR.glob("*.md")
        if p.is_file() and p.name not in NOT_ENTRIES
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="list pending fragments and exit; change nothing")
    args = ap.parse_args()

    pending = fragments()
    if not pending:
        print("no pending fragments in changelog.d/")
        return 0

    if args.check:
        print(f"{len(pending)} pending fragment(s):")
        for p in pending:
            print(f"  {p.relative_to(REPO)}")
        return 0

    text = CHANGELOG.read_text()
    lines = text.splitlines()
    anchors = [i for i, ln in enumerate(lines) if ln.strip() == ANCHOR]
    if len(anchors) != 1:
        # Not hypothetical: this file carried TWO identical `## Unreleased`
        # headings, which made "insert after the heading" ambiguous and broke a
        # naive insert. Refuse rather than guess which one was meant.
        print(f"refusing: CHANGELOG.md has {len(anchors)} '{ANCHOR}' headings "
              f"(lines {[i + 1 for i in anchors]}); expected exactly 1.",
              file=sys.stderr)
        return 1

    body = "\n\n".join(p.read_text().strip() for p in pending)
    at = anchors[0] + 1
    CHANGELOG.write_text(
        "\n".join(lines[:at] + ["", body] + lines[at:]).rstrip("\n") + "\n"
    )
    for p in pending:
        p.unlink()
    print(f"folded {len(pending)} fragment(s) into CHANGELOG.md:")
    for p in pending:
        print(f"  {p.relative_to(REPO)}")
    print("\nreview the diff, then commit CHANGELOG.md and the deletions together.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
