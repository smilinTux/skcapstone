#!/usr/bin/env python3
"""Classify agent projections without changing the projection store.

The report is deliberately diagnostic-only.  Conflict copies and malformed
records are reported as inputs, never adopted as canonical worker truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFLICT = ".sync-conflict-"
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def classify(path: Path, *, now: datetime, stale_after: int) -> dict[str, Any]:
    name = path.name
    result: dict[str, Any] = {
        "path": str(path),
        "filename": name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    if CONFLICT in name:
        result["disposition"] = "conflict-copy"
        return result
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result.update(disposition="malformed", error=type(exc).__name__)
        return result
    if not isinstance(payload, dict):
        result.update(disposition="malformed", error="not-an-object")
        return result
    agent = payload.get("agent")
    card = payload.get("current_task")
    result.update(agent=agent, current_task=card)
    if not isinstance(agent, str) or not agent:
        result.update(disposition="malformed", error="missing-agent")
        return result
    if not AGENT_RE.fullmatch(agent) or agent != path.stem:
        result.update(disposition="identity-mismatch", expected=path.stem)
        return result
    if card is not None and (not isinstance(card, str) or card == ""):
        result.update(disposition="malformed", error="invalid-current-task")
        return result
    seen = _iso(payload.get("last_seen"))
    if seen is None:
        result.update(disposition="malformed", error="invalid-last-seen")
        return result
    age = max(0, int((now - seen).total_seconds()))
    result["age_seconds"] = age
    result["disposition"] = "stale" if age > stale_after else "canonical"
    return result


def build_report(root: Path, *, now: datetime, stale_after: int) -> dict[str, Any]:
    files = sorted(root.glob("*.json"), key=lambda item: item.name)
    records = [classify(path, now=now, stale_after=stale_after) for path in files]
    counts: dict[str, int] = {}
    for record in records:
        key = str(record["disposition"])
        counts[key] = counts.get(key, 0) + 1
    return {
        "schema_version": "1",
        "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "root": str(root),
        "stale_after_seconds": stale_after,
        "source_mutated": False,
        "counts": dict(sorted(counts.items())),
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="canonical agent projection directory")
    parser.add_argument("--stale-after", type=int, default=900)
    parser.add_argument("--now", type=str, help="fixed ISO-8601 time for reproducible reports")
    args = parser.parse_args(argv)
    if args.stale_after < 0:
        parser.error("--stale-after must be non-negative")
    now = _iso(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        parser.error("--now must be ISO-8601")
    if not args.root.is_dir():
        print(f"projection root is not a directory: {args.root}", file=sys.stderr)
        return 2
    report = build_report(args.root, now=now, stale_after=args.stale_after)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
