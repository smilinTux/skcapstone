#!/usr/bin/env python3
"""Fail when one document states the same registered fact with two different values.

The bug this exists for: a note whose table said the codex pool ceiling was 32 while
a paragraph three above it said 4. The cap had been raised 4 -> 32 and only the table
was updated. Three readers in a row reasoned from the stale half, and nothing caught it,
because nothing compared a document against itself.

Generic prose contradiction detection is not tractable and this does not attempt it.
What IS tractable is a REGISTERED set of facts: a name, and a regex that finds every
place a document states that fact's value. Within one file, all extracted values must
agree. Across files, single-declaration is enforced separately by tier-3 assertions in
SOP.md; this checker is deliberately scoped to the self-contradiction case, because a
document that disagrees with itself is trusted by whoever reads the wrong half first.

Adding a fact is one line in FACTS. Keep the pattern narrow: a loose pattern produces
false positives, a CI gate that cries wolf gets waived, and a waived gate is decorative.

Usage:  check_self_consistency.py [path ...]      (default: docs/ SOP.md README.md)
Exit 0 = no document contradicts itself.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# name -> pattern with exactly one capturing group holding the value.
# `.` and newline end a match window on purpose: it keeps a pattern from spanning
# two unrelated sentences and inventing a contradiction.
FACTS: dict[str, str] = {
    "codex gateway pool ceiling": r"codex[^.\n]{0,60}?(?:max|ceiling|slots?)\b[^.\n]{0,20}?(\d{1,4})",
    "zai/glm gateway pool ceiling": r"\bzai\b[^.\n]{0,60}?(?:max|ceiling|slots?)\b[^.\n]{0,20}?(\d{1,4})",
    "chiap08-qwen38 pool ceiling": r"chiap08-qwen38[^.\n]{0,40}?(?:max|slots?)\b[^.\n]{0,15}?(\d{1,4})",
    "codex lane default model": r"(?:codex lane|SKFLEET_CODEX_LANE_MODEL)[^.\n]{0,60}?`(sk-codex[a-z-]*)`",
    "builder dispatch ceiling": r"BUILDER_CAPACITY[^.\n]{0,40}?(\d{1,4})",
    "chi fleet gateway port": r"chiap01[^.\n\s]{0,4}?:(\d{4,5})",
}

# The docs-evidence block in SOP.md holds the assertions themselves, which quote these
# very patterns and values. Comparing a checker against its own source is noise.
EVIDENCE = re.compile(r"<!--\s*docs-evidence.*?^-->", re.S | re.M)


def scan(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.name == "SOP.md":
        text = EVIDENCE.sub("", text)
    problems = []
    for fact, pattern in FACTS.items():
        seen: dict[str, int] = {}
        for n, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(pattern, line, re.I):
                seen.setdefault(m.group(1), n)
        if len(seen) > 1:
            where = ", ".join(f"{v!r} (line {ln})" for v, ln in sorted(seen.items(), key=lambda kv: kv[1]))
            problems.append(f"{path}: '{fact}' is stated as {where} in the SAME document")
    return problems


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("docs"), Path("SOP.md"), Path("README.md")]
    files: list[Path] = []
    for r in roots:
        files.extend(sorted(r.rglob("*.md")) if r.is_dir() else ([r] if r.is_file() else []))
    problems = [p for f in files for p in scan(f)]
    for p in problems:
        print(f"  FAIL {p}")
    if not problems:
        print(f"  ok   no document contradicts itself on {len(FACTS)} registered facts "
              f"({len(files)} files)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
