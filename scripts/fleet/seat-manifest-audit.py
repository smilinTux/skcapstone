#!/usr/bin/env python3
"""Check lifecycle seat identity and role manifests without mutating them."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skcapstone.seat_manifest_audit import SEATS, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--seat", action="append", choices=SEATS)
    args = parser.parse_args()
    result = report(args.home, args.seat or SEATS)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
