"""Fixed read-only readiness diagnostic in the native worker service context."""

import json
import sys

from . import sandbox_tools


def main() -> int:
    """Run installed prerequisite checks, without card source or launch authority."""
    failures = sandbox_tools.readiness(sys.executable)
    print(json.dumps({"schema": "skfleet.sandbox-readiness/v1", "failures": failures}))
    return int(bool(failures))


if __name__ == "__main__":
    if len(sys.argv) != 1:
        raise SystemExit("sandbox probe accepts no arguments")
    raise SystemExit(main())
