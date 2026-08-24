#!/usr/bin/env python3
"""Capture the exact SKCP-00 V1.1.2 review closure without board writes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "docs" / "review"
CAPTURE = REVIEW / "SKCP-00-RELEVANT-BOARD-CAPTURE-v1.1.2.json"
ROOT_IDS = ["26c69f86", "4e1130cc", "7888e091", "c3a9c9e9", "eddaa1fb"]


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(card_id: str, home: Path) -> dict[str, object]:
    card_root = home / "cards" / card_id
    core = card_root / "core.json"
    logs = []
    for log in sorted((card_root / "events").glob("*.jsonl")):
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
        logs.append(
            {
                "path": log.relative_to(home).as_posix(),
                "sha256": sha256(log),
                "event_count": len(events),
                "events": events,
            }
        )
    return {
        "id": card_id,
        "core_path": core.relative_to(home).as_posix(),
        "core_sha256": sha256(core),
        "event_logs": logs,
    }


def main() -> int:
    result = subprocess.run(
        ["skcapstone", "coord", "kanban", "--json"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit("kanban capture failed")
    board = json.loads(result.stdout)
    cards = [card for lane in board.values() for column in lane.values() for card in column]
    by_id = {card["id"]: card for card in cards}
    closure = set(ROOT_IDS)
    pending = list(ROOT_IDS)
    while pending:
        card_id = pending.pop()
        if card_id not in by_id:
            raise SystemExit(f"missing captured card: {card_id}")
        for dependency in by_id[card_id]["dependencies"]:
            if dependency not in closure:
                closure.add(dependency)
                pending.append(dependency)
    home = Path(os.environ.get("SKCAPSTONE_HOME", Path.home() / ".skcapstone"))
    selected = []
    edges = []
    for card_id in sorted(closure):
        source = by_id[card_id]
        dependencies = list(source["dependencies"])
        selected.append(
            {
                "id": card_id,
                "title": source["title"],
                "kind": source["kind"],
                "status": source["status"],
                "swimlane": source["swimlane"],
                "priority": source["priority"],
                "labels": source["labels"],
                "acceptance_criteria": source["acceptance_criteria"],
                "folded_dependencies": dependencies,
                "internal_dependency_ids": [item for item in dependencies if item in closure],
                "external_dependency_ids": [item for item in dependencies if item not in closure],
                "provenance": provenance(card_id, home),
            }
        )
        edges.extend(
            {
                "from": card_id,
                "to": dependency,
                "classification": "internal" if dependency in closure else "external",
            }
            for dependency in dependencies
        )
    edges.sort(key=lambda item: (item["from"], item["to"]))
    subset = {"cards": selected, "edges": edges}
    capture = {
        "capture_version": "1.1.0",
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "command": "skcapstone coord kanban --json",
        "command_exit_status": result.returncode,
        "raw_projection": {
            "sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
            "card_count": len(by_id),
            "published": False,
            "reason": "The full projection contains unrelated board content. Only the exact closure is published.",
        },
        "closure_algorithm": {
            "roots": ROOT_IDS,
            "method": "Recursively include every folded dependency from one frozen kanban response.",
            "canonical_subset_rule": "SHA256 of sorted-key compact UTF-8 JSON containing cards and edges.",
        },
        "closure": {
            "root_count": len(ROOT_IDS),
            "node_count": len(selected),
            "internal_dependency_edge_count": sum(edge["classification"] == "internal" for edge in edges),
            "missing_ids": [],
            "status_counts": dict(sorted(Counter(card["status"] for card in selected).items())),
            "closure_ids": sorted(closure),
        },
        "cards": selected,
        "edges": edges,
        "canonical_subset_sha256": hashlib.sha256(canonical_bytes(subset)).hexdigest(),
    }
    CAPTURE.write_text(json.dumps(capture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
