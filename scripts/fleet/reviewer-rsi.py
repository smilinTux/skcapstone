#!/usr/bin/env python3
"""Print the deterministic reviewer quality projection as JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skcapstone.reviewer_rsi import project_from_card_store


def main() -> int:
    """Run the read-only projection and return a process status."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(project_from_card_store(args.home), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
