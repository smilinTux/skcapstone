#!/usr/bin/env python3
"""Build immutable, byte-addressed replacements for fleet audit reports.

Mutable JSONL inputs are copied byte for byte. Every copied line is parsed before
publication. All inputs also receive a prefix-bound manifest entry, so later
append-only growth does not change the reviewed prefix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home() / ".skcapstone"
CARD_ID = "c84987dd"
PARTITIONS = {
    "01": ("6f351b87", "partition-01-report.json", "8d3e5ed8", "independent-review.json"),
    "04": ("53d0c7fc", "partition-04-audit.json", "3569c4a6", "BLOCKED-review.json"),
    "05": ("77c8514b", "partition-05-audit.json", "068d8a8b", "independent-review.json"),
    "14": ("2076c423", "partition-14-audit.json", "83e04cf1", "partition-14-independent-review.json"),
    "18": ("d7d7f9a7", "partition-18-report.json", "c91897c8", "review.json"),
}
PATH_TOKEN = re.compile(r"/home/[A-Za-z0-9_.@/+*-]+")


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> Any:
    with path.open("rb") as handle:
        return json.load(handle)


def path_strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from path_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from path_strings(item)
    elif isinstance(value, str):
        yield from PATH_TOKEN.findall(value)


def read_stable(path: Path, attempts: int = 5) -> tuple[bytes, os.stat_result]:
    for _ in range(attempts):
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) and len(data) == before.st_size:
            return data, before
    raise RuntimeError(f"source changed while being captured: {path}")


def parse_jsonl(data: bytes, path: Path) -> int:
    count = 0
    for count, line in enumerate(data.splitlines(), 1):
        try:
            json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid JSONL at {path}:{count}: {exc}") from exc
    return count


def source_entry(path: Path, snapshots: Path) -> dict[str, Any]:
    data, stat = read_stable(path)
    digest = sha256(data)
    lines = None
    snapshot = None
    snapshot_digest = None
    if path.suffix == ".jsonl":
        lines = parse_jsonl(data, path)
        relative = Path(digest[:2]) / f"{digest}.jsonl"
        target = snapshots / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise RuntimeError(f"digest collision at {target}")
        if not target.exists():
            target.write_bytes(data)
        snapshot = str(target)
        snapshot_digest = sha256(target.read_bytes())
    return {
        "source_path": str(path),
        "source_kind": "append_only_jsonl" if path.suffix == ".jsonl" else "file",
        "cutoff_bytes": len(data),
        "cutoff_json_lines": lines,
        "prefix_sha256": digest,
        "exact_bytes_sha256": digest,
        "snapshot_path": snapshot,
        "snapshot_sha256": snapshot_digest,
        "provenance": {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "mode": oct(stat.st_mode & 0o7777),
            "mtime_ns_at_capture": stat.st_mtime_ns,
        },
    }


def remedy_proposals(report: Any) -> list[Any]:
    found: list[Any] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = key.lower()
                if "remed" in lowered or "propos" in lowered or lowered in {
                    "next_action",
                    "supported_action",
                }:
                    found.append({"field": key, "value": item})
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(report)
    return found


def build(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    snapshots = output / "source-snapshots"
    reports = output / "replacement-reports"
    immutable_reports = output / "immutable" / "reports"
    reports.mkdir(exist_ok=True)
    immutable_reports.mkdir(parents=True, exist_ok=True)
    captured: dict[str, dict[str, Any]] = {}
    replacements: list[dict[str, Any]] = []
    captured_at = datetime.now(timezone.utc).isoformat()

    for partition, (source_id, report_name, review_id, review_name) in PARTITIONS.items():
        report_path = HOME / "evidence" / "work" / source_id / report_name
        review_path = HOME / "evidence" / "work" / review_id / review_name
        report = load_json(report_path)
        review = load_json(review_path)
        cited = set(path_strings(report)) | set(path_strings(review))
        cited |= {str(report_path), str(review_path)}
        entries: list[dict[str, Any]] = []
        absent: list[str] = []
        for raw in sorted(cited):
            path = Path(raw.rstrip(".,;:|"))
            try:
                exists = path.is_file()
            except OSError:
                exists = False
            if not exists:
                absent.append(str(path))
                continue
            key = str(path)
            if key not in captured:
                captured[key] = source_entry(path, snapshots)
            entries.append(captured[key])

        replacement = {
            "schema": "skcapstone.fleet_unblock_immutable_audit.v1",
            "repair_card_id": CARD_ID,
            "partition": int(partition),
            "source_card_id": source_id,
            "blocked_review_card_id": review_id,
            "captured_at_utc": captured_at,
            "source_report": {
                "path": str(report_path),
                "bytes": report_path.stat().st_size,
                "sha256": sha256(report_path.read_bytes()),
            },
            "blocked_review": {
                "path": str(review_path),
                "bytes": review_path.stat().st_size,
                "sha256": sha256(review_path.read_bytes()),
            },
            "methodology": {
                "structural_store": "CardStore core and event JSONL bytes named in source_manifest",
                "evidence_store": "evidence events, linked artifact bytes, and sanctioned legacy evidence bytes named in source_manifest",
                "join_rule": "Structural state and evidence outcomes remain separate. No verdict is inferred from lifecycle state or links.",
                "cutoff_rule": "For each source, inspect exactly cutoff_bytes and verify prefix_sha256. Ignore later suffix bytes.",
                "mutable_source_rule": "Every cited JSONL source has an immutable exact-byte snapshot and a parsed line cutoff.",
                "human_gate_rule": "Only explicit Chef approval or explicit Chef void can discharge a HUMAN gate.",
            },
            "source_manifest": entries,
            "unresolved_path_tokens": absent,
            "supported_remedy_proposals": remedy_proposals(report),
            "complete_two_store_join_report": report,
            "verdict": "PASS_FOR_REVIEW",
            "mutations_applied": [],
        }
        data = canonical_bytes(replacement)
        target = reports / f"partition-{partition}-immutable-report.json"
        target.write_bytes(data)
        digest = sha256(data)
        immutable_target = immutable_reports / f"{digest}.json"
        if immutable_target.exists() and immutable_target.read_bytes() != data:
            raise RuntimeError(f"digest collision at {immutable_target}")
        immutable_target.write_bytes(data)
        sidecar = target.with_suffix(target.suffix + ".sha256")
        sidecar.write_text(f"{digest}  {target.name}\n", encoding="utf-8")
        replacements.append(
            {
                "partition": int(partition),
                "path": str(target),
                "immutable_path": str(immutable_target),
                "bytes": len(data),
                "sha256": digest,
                "sidecar": str(sidecar),
            }
        )

    manifest = {
        "schema": "skcapstone.fleet_unblock_immutable_bundle.v1",
        "repair_card_id": CARD_ID,
        "captured_at_utc": captured_at,
        "source_count": len(captured),
        "mutable_snapshot_count": sum(1 for entry in captured.values() if entry["snapshot_path"]),
        "sources": [captured[key] for key in sorted(captured)],
        "replacement_reports": replacements,
        "mutations_applied": [],
    }
    manifest_data = canonical_bytes(manifest)
    manifest_path = output / "bundle-manifest.json"
    manifest_path.write_bytes(manifest_data)
    manifest_digest = sha256(manifest_data)
    (output / "bundle-manifest.json.sha256").write_text(
        f"{manifest_digest}  bundle-manifest.json\n", encoding="utf-8"
    )
    immutable_manifest = output / "immutable" / "manifests" / f"{manifest_digest}.json"
    immutable_manifest.parent.mkdir(parents=True, exist_ok=True)
    if immutable_manifest.exists() and immutable_manifest.read_bytes() != manifest_data:
        raise RuntimeError(f"digest collision at {immutable_manifest}")
    immutable_manifest.write_bytes(manifest_data)

    for path in output.rglob("*.json"):
        load_json(path)
    for entry in captured.values():
        if entry["snapshot_path"]:
            data = Path(entry["snapshot_path"]).read_bytes()
            if sha256(data) != entry["snapshot_sha256"]:
                raise RuntimeError(f"snapshot digest mismatch: {entry['snapshot_path']}")
            parse_jsonl(data, Path(entry["snapshot_path"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=HOME / "evidence" / "work" / CARD_ID,
    )
    args = parser.parse_args()
    build(args.output.resolve())


if __name__ == "__main__":
    main()
