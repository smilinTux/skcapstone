#!/usr/bin/env python3
"""Emit SKRSI observations for fleet heartbeat freshness.

SKRSI ships contracts and bounded components but deliberately NO scheduler:
docs/ARCHITECTURE.md states the target registry "does not own runtime
activation", and the repo "does not provide a scheduler". Activation is the
adopter's job. This is that job, for one target, done the smallest honest way.

WHAT IT MEASURES
    Per fleet node, the age in milliseconds of its last self-report
    (``fleet/status/<node>/node.json``). Heartbeat freshness is a real SLI: it
    is exactly what was silently wrong on node-41, which reported every 300s
    against a shorter liveness threshold and therefore read as Dead between
    beats while being perfectly healthy.

DATA BOUNDARY
    Metadata only, allowlisted keys only. The collector hashes every string
    value on the way in (skrsi_collector._hash_strings), so node names and
    roles persist as sha256 correlation handles rather than clear text, while
    the numeric measurement stays readable. No bodies, no prompts, no
    credentials, no paths.

BOUNDARY OF AUTHORITY
    Observation only. This never approves, merges, deploys, or mutates any
    authority store. It appends to an append-only outbox and exits.

STATE LOCATION
    ``--state-dir`` must be host-local and NOT inside the Syncthing-synced
    ~/.skcapstone tree: two hosts converging on one append-only journal is the
    exact conflict the outbox fails closed on.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from skcapstone.skrsi_collector import BoundedCollector
from skcapstone.skrsi_registry import AppendOnlyOutbox

TARGET_REF = "nor-fleet-heartbeat"


def node_digest(node: str) -> str:
    """Stable, non-reversing handle for a node name.

    The fence needs STABILITY, not readability. _hash_strings covers metadata
    values but not natural_key / event_id, so a clear node name would otherwise
    be persisted in the outbox and in every idempotency key. The adopter guide
    is explicit: "Hash opaque identifiers when an aggregate does not require
    their clear value", and an aggregate over heartbeat age does not.

    Same digest as the collector uses for metadata strings, so
    ``metadata.host`` and this key correlate, and an operator holding the node
    list can still map back locally.
    """
    return hashlib.sha256(node.encode()).hexdigest()[:16]


SOURCE = "skfleet"


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def node_observations(home: Path, now: datetime) -> list[dict]:
    """One envelope per fleet node, carrying only allowlisted metadata.

    Event-time model, and it is deliberate. The collector enforces a single
    strictly-increasing cursor per source and a non-decreasing ``occurred_at``
    (skrsi_collector.drain: "stale cursor", "timestamp regressed"). N nodes
    self-reporting at N different times cannot be replayed as N event times
    without regressing that cursor, so ``occurred_at`` is the COLLECTION
    instant, identical for every node in a cycle, which the contract permits.

    That is not a workaround, it is the truthful reading: what this measures is
    "as of now, node X's last beat was N ms ago". The node's own reportedAt is
    the MEASUREMENT (duration_ms), not the event time.

    ``natural_key`` is fenced on (node, COLLECTION stamp), not on the beat.
    Fencing it on the beat looks tempting and is wrong: the collector hashes
    the whole envelope and rejects "same natural key, different bytes" as a
    natural key conflict, so a fixed key with a moving cursor made every cycle
    after the first fail. Observed directly: run 2 rejected 3 of 3 and
    dead-lettered them into the outbox. Since each cycle IS a distinct
    observation of "age as of now", the cycle belongs in the key. A retry
    inside the same second is a true duplicate and still dedupes.

    A node whose report cannot be parsed is emitted with ``quality="partial"``
    rather than skipped: the adopter guide requires missing data stay explicit
    and never silently become zero.
    """
    out: list[dict] = []
    status_dir = home / "fleet" / "status"
    if not status_dir.is_dir():
        return out
    # The fence key and the fenced bytes MUST share resolution. natural_key is
    # built from whole seconds, so occurred_at is quantized to whole seconds
    # too. With microsecond timestamps, two runs inside the same second produced
    # the SAME natural_key with DIFFERENT envelope bytes, which the collector
    # correctly rejects as "natural key conflict" (observed: run 2 rejected 3 of
    # 3 and dead-lettered them). Quantized, that same retry is byte-identical
    # and dedupes, which is the idempotency the fence exists to provide.
    now = now.replace(microsecond=0)
    stamp = int(now.timestamp())
    for index, node_file in enumerate(sorted(status_dir.glob("*/node.json"))):
        node = node_file.parent.name
        try:
            data = json.loads(node_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        reported = _iso(data.get("reportedAt") or data.get("updatedAt"))
        # Strictly increasing across this cycle AND every later cycle.
        cursor = f"{stamp:012d}{index:03d}"
        if reported is None:
            out.append(
                {
                    "source": SOURCE,
                    "natural_key": f"{node_digest(node)}-unparsed-{stamp}",
                    "event_id": f"{node_digest(node)}-unparsed-{stamp}",
                    "event_type": "skrsi.fleet_heartbeat",
                    "cursor": cursor,
                    "occurred_at": now.isoformat(),
                    "target_ref": TARGET_REF,
                    "quality": "partial",
                    "metadata": {"host": node, "count": 1},
                }
            )
            continue
        age_ms = max(0.0, (now - reported).total_seconds() * 1000.0)
        spec = (data.get("spec") or {}).get("spec") or {}
        out.append(
            {
                "source": SOURCE,
                "natural_key": f"{node_digest(node)}-{stamp}",
                "event_id": f"{node_digest(node)}-{stamp}",
                "event_type": "skrsi.fleet_heartbeat",
                "cursor": cursor,
                "occurred_at": now.isoformat(),
                "target_ref": TARGET_REF,
                "quality": "complete",
                "metadata": {
                    "host": node,
                    "category": str(spec.get("role") or "unknown"),
                    # Numeric, so it survives _hash_strings as a real value.
                    # The strings around it are correlation handles only.
                    "duration_ms": round(age_ms, 3),
                    "count": 1,
                },
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local" / "state" / "skrsi",
        help="Host-local. Must NOT be inside the Syncthing-synced tree.",
    )
    parser.add_argument("--target-revision", default="1")
    parser.add_argument("--json", action="store_true", help="Machine-readable result.")
    args = parser.parse_args(argv)

    if (
        args.state_dir.resolve() == args.home.resolve()
        or args.home.resolve() in args.state_dir.resolve().parents
    ):
        parser.error(
            f"--state-dir {args.state_dir} is inside --home {args.home}; SKRSI state must be "
            "host-local and outside the Syncthing tree"
        )

    now = datetime.now(timezone.utc)
    outbox = AppendOnlyOutbox(args.state_dir / "outbox.jsonl")
    collector = BoundedCollector(
        outbox,
        source=SOURCE,
        target_ref=TARGET_REF,
        authority="SKFleet",
        handoff_owner="niobe",
        target_revision=args.target_revision,
    )

    submitted = 0
    for envelope in node_observations(args.home, now):
        if collector.submit(envelope):
            submitted += 1

    result = collector.drain(now=now)
    payload = {
        "submitted": submitted,
        "accepted": result.accepted,
        "rejected": result.rejected,
        "duplicates": result.duplicates,
        "overloaded": result.overloaded,
        "cursor": result.cursor,
        "measurements": len(result.measurements),
        "outbox": str(outbox.path),
        "outbox_entries": len(outbox.entries()),
    }
    if args.json:
        print(json.dumps(payload))
    else:
        print(
            f"skrsi fleet: submitted={submitted} accepted={result.accepted} "
            f"duplicates={result.duplicates} rejected={result.rejected} "
            f"outbox={len(outbox.entries())} entries"
        )
        for m in result.measurements:
            print(f"  {m.name}={m.value}{m.unit} samples={m.sample_count} quality={m.quality}")
    # Rejections are a real signal, not noise: fail loudly so a timer surfaces it.
    return 1 if result.rejected else 0


if __name__ == "__main__":
    sys.exit(main())
