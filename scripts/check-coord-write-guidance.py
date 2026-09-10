#!/usr/bin/env python3
"""Reject fleet instructions that tell workers to mutate CardStore JSONL directly."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "scripts" / "fleet",
    ROOT / "systemd",
    ROOT / "src" / "skcapstone" / "codex_setup.py",
    ROOT / "src" / "skcapstone" / "context_loader.py",
    ROOT / "src" / "skcapstone" / "crush_integration.py",
    ROOT / "src" / "skcapstone" / "lightweight.py",
    ROOT / "src" / "skcapstone" / "defaults" / "claude",
)
FORBIDDEN = re.compile(
    r"(?i)(?:write|append|edit|mutate)[^\n]{0,120}"
    r"(?:card_events|cards/.+/events)[^\n]{0,80}\.jsonl"
)


def main() -> int:
    """Report forbidden raw-write instructions in fleet launch and prompt surfaces."""
    failures = []
    for root in TARGETS:
        paths = (root,) if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix in {".png", ".jpg", ".svg"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if FORBIDDEN.search(line) and not re.search(
                    r"(?i)\b(?:never|do not|must not|forbid)", line
                ):
                    try:
                        shown = path.relative_to(ROOT)
                    except ValueError:
                        shown = path
                    failures.append(f"{shown}:{line_number}: {line.strip()}")
    if failures:
        print("Raw CardStore JSONL write instruction found. Use skcapstone coord:\n")
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
