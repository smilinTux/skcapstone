"""Fleet dependency parity: installed versions must match deploy/fleet-constraints.txt.

Incident 2026-10-13: a PR used a skcoord symbol that existed in the 0.1.87
installed on every host but not in the 0.1.86 pyproject floor CI resolved to.
CI failed; had the floor instead been above production, production would have
broken while CI passed. The fix is one file naming the production versions,
consumed by CI (``pip -c``), rollout (``pip -c``), and this gate.

Runnable by path with only the standard library plus ``packaging`` so a
rollout can use the freshly pulled checkout's copy, including during rollback.
Exit 0 on parity, 3 on drift (message starts ``DEPENDENCY_DRIFT``), 2 on a
bad constraints file.
"""

from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

DEFAULT_CONSTRAINTS = Path(__file__).resolve().parents[3] / "deploy" / "fleet-constraints.txt"


def parse_constraints(text: str) -> dict[str, Version]:
    """Parse exact pins (``name==version``) into ``{canonical_name: Version}``."""
    pins: dict[str, Version] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement as exc:
            raise ValueError(f"line {number}: {exc}") from exc
        specs = list(req.specifier)
        if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
            raise ValueError(f"line {number}: {line!r} is not an exact == pin")
        pins[canonicalize_name(req.name)] = Version(specs[0].version)
    return pins


def load_constraints(path: Path = DEFAULT_CONSTRAINTS) -> dict[str, Version]:
    return parse_constraints(Path(path).read_text(encoding="utf-8"))


def installed_version(name: str) -> Version | None:
    try:
        return Version(metadata.version(name))
    except metadata.PackageNotFoundError:
        return None


def find_drift(pins: dict[str, Version]) -> list[str]:
    """One human line per pinned distribution whose installed version differs."""
    drift = []
    for name, want in sorted(pins.items()):
        have = installed_version(name)
        if have != want:
            drift.append(f"{name}: installed {have or 'MISSING'}, constraints pin {want}")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--constraints", type=Path, default=DEFAULT_CONSTRAINTS)
    args = parser.parse_args(argv)
    try:
        pins = load_constraints(args.constraints)
    except (OSError, ValueError) as exc:
        print(f"DEPENDENCY_CONSTRAINTS_UNREADABLE: {args.constraints}: {exc}", file=sys.stderr)
        return 2
    drift = find_drift(pins)
    if drift:
        print(
            "DEPENDENCY_DRIFT: installed environment differs from "
            f"{args.constraints}: " + "; ".join(drift),
            file=sys.stderr,
        )
        return 3
    print(f"dependency parity ok: {', '.join(f'{n}=={v}' for n, v in sorted(pins.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
