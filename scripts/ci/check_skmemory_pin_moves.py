#!/usr/bin/env python3
"""Fail a PR that bumps skmemory in pyproject/constraints without moving the fleet pin.

Usage: check_skmemory_pin_moves.py BASE_REF   (run inside a checkout of the PR head)

Rule 1: if the skmemory requirement lines in pyproject.toml or any other
constraints*.txt differ from BASE_REF, deploy/fleet-constraints.txt's
skmemory pin must differ too, in the same PR.
Rule 2: no skmemory floor (>=X) in pyproject.toml may exceed the pin.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from packaging.version import Version

FLEET = "deploy/fleet-constraints.txt"
_LINE = re.compile(r"^\s*[\"']?\s*skmemory\b[^\"',\n]*", re.I | re.M)
_PIN = re.compile(r"^\s*skmemory\s*==\s*([^\s#]+)", re.I | re.M)
_FLOOR = re.compile(r"skmemory(?:\[[^\]]*\])?\s*>=\s*([0-9][^\s\"',;]*)", re.I)


def skmemory_lines(text: str) -> list[str]:
    return sorted(m.group(0).strip().strip("\"'").strip() for m in _LINE.finditer(text))


def fleet_pin(text: str) -> str | None:
    m = _PIN.search(text)
    return m.group(1) if m else None


def check(
    changed: dict[str, tuple[str, str]], fleet_base: str, fleet_head: str, pyproject_head: str
) -> list[str]:
    """changed maps path -> (base_text, head_text) for pyproject and other constraints."""
    errors = []
    bumped = [p for p, (b, h) in changed.items() if skmemory_lines(b) != skmemory_lines(h)]
    if bumped and fleet_pin(fleet_base) == fleet_pin(fleet_head):
        errors.append(
            f"skmemory changed in {', '.join(bumped)} but the skmemory pin in {FLEET} "
            f"did not move ({fleet_pin(fleet_head)}); move it in the same PR"
        )
    pin = fleet_pin(fleet_head)
    if pin:
        for floor in _FLOOR.findall(pyproject_head):
            if Version(floor) > Version(pin):
                errors.append(f"skmemory floor >={floor} exceeds the fleet pin =={pin}")
    return errors


def _show(ref: str, path: str) -> str:
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "origin/main"
    paths = ["pyproject.toml"] + sorted(
        str(p) for p in Path(".").glob("**/constraints*.txt") if ".git" not in p.parts
    )
    paths = [p for p in paths if p != FLEET]
    changed = {p: (_show(base, p), Path(p).read_text()) for p in paths if Path(p).exists()}
    errors = check(
        changed, _show(base, FLEET), Path(FLEET).read_text(), Path("pyproject.toml").read_text()
    )
    for e in errors:
        print(f"::error::{e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
