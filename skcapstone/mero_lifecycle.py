"""Fail-closed lifecycle invariants for the Mero selector pipeline.

This module is deliberately storage-neutral.  Callers provide one bounded,
immutable snapshot, structural lifecycle events, and separate evidence events.
No lifecycle status or link is treated as a verdict by itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping

_ALLOWED_FALSE = frozenset(
    {
        "review",
        "terminal",
        "human",
        "sensitive",
        "superseded",
        "owned",
        "stale",
        "unknown",
        "malformed",
        "drifted",
    }
)


@dataclass(frozen=True)
class LifecycleHealth:
    snapshot_revision: str
    stage_counts: Mapping[str, int]
    unmatched_ids: Mapping[str, tuple[str, ...]]
    invariants: Mapping[str, str]
    remediation_owner: str = "mero"

    def to_json(self) -> str:
        """Serialize compact, non-protected health evidence deterministically."""
        return json.dumps(
            {
                "schema": "skfleet.mero-health/v1",
                "snapshot_revision": self.snapshot_revision,
                "stage_counts": dict(sorted(self.stage_counts.items())),
                "unmatched_ids": {k: list(v) for k, v in sorted(self.unmatched_ids.items())},
                "invariants": dict(sorted(self.invariants.items())),
                "remediation_owner": self.remediation_owner,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


def _ids(rows: Iterable[Mapping[str, object]]) -> set[str]:
    result = set()
    for row in rows:
        card_id = row.get("card_id") or row.get("id")
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("lifecycle row requires card_id")
        if card_id in result:
            raise ValueError(f"duplicate card_id in lifecycle stage: {card_id}")
        result.add(card_id)
    return result


def _bounded(values: set[str], limit: int) -> tuple[str, ...]:
    return tuple(sorted(values)[:limit])


def reconcile_snapshot(
    snapshot_revision: str,
    stages: Mapping[str, Iterable[Mapping[str, object]]],
    *,
    max_ids: int = 20,
) -> LifecycleHealth:
    """Reconcile adjacent stages from one bounded snapshot and fail closed.

    A stage is materialized exactly once.  This prevents a caller from
    accidentally comparing different reads, and lets us detect population
    loss, gain, and classification drift rather than only count changes.
    """
    if not snapshot_revision or max_ids < 1:
        raise ValueError("snapshot revision and positive ID bound are required")
    materialized = {name: tuple(rows) for name, rows in stages.items()}
    stage_ids = {name: _ids(rows) for name, rows in materialized.items()}
    names = list(stage_ids)
    unmatched: dict[str, tuple[str, ...]] = {}
    invariants: dict[str, str] = {}
    for left, right in zip(names, names[1:]):
        lost = stage_ids[left] - stage_ids[right]
        gained = stage_ids[right] - stage_ids[left]
        left_by_id = {str(row.get("card_id") or row.get("id")): row for row in materialized[left]}
        right_by_id = {
            str(row.get("card_id") or row.get("id")): row for row in materialized[right]
        }
        drifted = {
            card_id
            for card_id in stage_ids[left] & stage_ids[right]
            # Classification must be carried consistently by both stages. A
            # missing category is drift, not permission to silently relabel.
            if (
                "category" not in left_by_id[card_id]
                or "category" not in right_by_id[card_id]
                or left_by_id[card_id].get("category") != right_by_id[card_id].get("category")
            )
        }
        key = f"{left}->{right}"
        if lost or gained or drifted:
            invariants[key] = "BLOCKED"
            if lost:
                unmatched[f"{key}:lost"] = _bounded(lost, max_ids)
            if gained:
                unmatched[f"{key}:gained"] = _bounded(gained, max_ids)
            if drifted:
                unmatched[f"{key}:category_changed"] = _bounded(drifted, max_ids)
        else:
            invariants[key] = "PASS"
    return LifecycleHealth(
        snapshot_revision, {k: len(v) for k, v in stage_ids.items()}, unmatched, invariants
    )


def review_dispatch_decision(
    structural: Mapping[str, object], evidence: Mapping[str, object], *, reviewer: str | None
) -> str:
    """Return dispatch only for a governed, fully evidenced distinct review."""
    if structural.get("claimable") is not False or structural.get("reason") != "review":
        return "BLOCKED"
    if evidence.get("review_label") is not True:
        return "BLOCKED"
    if not isinstance(evidence.get("producer"), str) or not evidence["producer"]:
        return "BLOCKED"
    digest = evidence.get("evidence_sha256")
    if not isinstance(digest, str):
        return "BLOCKED"
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return "BLOCKED"
    if not reviewer or reviewer == evidence["producer"]:
        return "BLOCKED"
    return "PASS_FOR_REVIEW"


def launch_decision(
    structural: Mapping[str, object],
    evidence: Mapping[str, object] | None = None,
    *,
    reviewer: str | None = None,
) -> str:
    """Return the only dispatchable outcome for a selector row.

    Structural lifecycle state is not evidence.  Consequently every false row
    is blocked unless it is the special review case and its independent
    evidence is supplied to :func:`review_dispatch_decision`.
    """
    if structural.get("claimable") is not False:
        return "BLOCKED"
    if structural.get("reason") != "review" or evidence is None:
        return "BLOCKED"
    return review_dispatch_decision(structural, evidence, reviewer=reviewer)


def false_state_launches_zero(rows: Iterable[Mapping[str, object]]) -> bool:
    """Ensure all non-review false states have zero launch opportunities."""
    return all(row.get("claimable") is not False or row.get("reason") != "review" for row in rows)
