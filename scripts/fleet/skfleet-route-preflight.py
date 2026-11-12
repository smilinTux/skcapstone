#!/usr/bin/env python3
"""Manual fleet route resolution and same-path preflight."""

import argparse
import json

from skcapstone.fleet_route_preflight import resolve_and_preflight


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("logical_route")
    parser.add_argument("--gateway", default="http://chiap01:18790")
    args = parser.parse_args()
    try:
        result = resolve_and_preflight(args.gateway, args.logical_route)
    except ValueError as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return 2
    print(json.dumps({"ok": True, **result.to_dict()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
