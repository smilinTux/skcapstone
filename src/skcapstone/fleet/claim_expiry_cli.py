"""Report which claims are past their idle deadline. Never writes.

This ships before any enforcement exists, so that the rollout's report only
phase has a tool. It reads the CardStore's event log through observe() and
evaluate(), formats what it finds, and exits 0 regardless of how many
claims are reclaimable: a report is not a failure, and nothing in CI should
go red because the fleet has expired claims.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from .claim_expiry import evaluate, mode_from_env, observe, ttl_seconds_from_env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skfleet-claim-expiry")
    ap.add_argument("--home", default=os.path.expanduser("~/.skcapstone"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reclaimable-only", action="store_true")
    args = ap.parse_args(argv)

    ttl = ttl_seconds_from_env(os.environ)
    mode = mode_from_env(os.environ)
    verdicts = evaluate(observe(Path(args.home)), now=time.time(), ttl_seconds=ttl)
    shown = [v for v in verdicts if v.reclaimable] if args.reclaimable_only else verdicts

    if args.json:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "ttl_hours": ttl / 3600.0,
                    "held_count": len(verdicts),
                    "reclaimable_count": sum(1 for v in verdicts if v.reclaimable),
                    "verdicts": [asdict(v) for v in shown],
                },
                indent=2,
            )
        )
        return 0

    print(
        f"mode={mode} ttl={ttl / 3600.0:.1f}h held={len(verdicts)} "
        f"reclaimable={sum(1 for v in verdicts if v.reclaimable)}"
    )
    for v in sorted(shown, key=lambda x: -x.idle_seconds):
        flag = "RECLAIM" if v.reclaimable else "hold   "
        print(
            f"  {flag} {v.card_id}  idle={v.idle_seconds / 3600.0:7.1f}h  "
            f"{v.owner[:34]:34} {v.reason}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
