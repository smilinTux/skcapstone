"""Append-only manual evidence records for deadlines and correspondence.

This module deliberately separates structural records from evidence.  A receipt,
tracking number, or lifecycle event is never promoted to a delivery, mailing, or
legal outcome.  Every JSONL line is serialized and parsed before it is appended,
so malformed existing data fails closed rather than being silently extended.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "skcapstone.legal-record.v1"
EVIDENCE_SCHEMA = "skcapstone.legal-evidence.v1"


def _tracking_claim(value: Any) -> bool:
    """Return whether tracking metadata is coupled to an inferred outcome.

    Tracking identifiers are retained as manual evidence only.  This check is
    recursive because receipt attachments are commonly nested in lists or
    envelope objects, and applies equally to later corrections.
    """
    forbidden = {
        "outcome", "status", "delivery_status", "mailing_status",
        "legal_outcome", "delivered", "mailed", "served",
    }
    if isinstance(value, dict):
        if "tracking_number" in value and any(
            key in value and value[key] not in (None, False, "")
            for key in forbidden
        ):
            return True
        return any(_tracking_claim(item) for item in value.values())
    if isinstance(value, list):
        return any(_tracking_claim(item) for item in value)
    return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: dict[str, Any]) -> str:
    """Serialize one object and parse it back before it can reach disk."""
    line = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    parsed = json.loads(line)
    if not isinstance(parsed, dict):
        raise ValueError("record must serialize as a JSON object")
    return line


def _append(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _json(value)
    # Validate every pre-existing line.  Never append to a corrupt JSONL store.
    if path.exists():
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if raw.strip():
                try:
                    existing = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON at {path}:{number}") from exc
                if not isinstance(existing, dict):
                    raise ValueError(f"non-object JSON at {path}:{number}")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    return json.loads(line)


def _event(kind: str, record_id: str, **fields: Any) -> dict[str, Any]:
    return {"schema": SCHEMA, "event_id": str(uuid.uuid4()), "kind": kind,
            "record_id": record_id, "occurred_at": _now(), "writer": os.environ.get("SKAGENT", "unknown"),
            "node": socket.gethostname(), **fields}


def append_record(root: Path, record_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Append a structural task/deadline/communication record."""
    if not record_id or not isinstance(record, dict):
        raise ValueError("record_id and object record are required")
    return _append(root / "records.jsonl", _event("record", record_id, record=dict(record)))


def append_evidence(root: Path, record_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Append evidence separately from structure, preserving supplied bytes/intent."""
    if not record_id or not isinstance(evidence, dict):
        raise ValueError("record_id and object evidence are required")
    if _tracking_claim(evidence):
        raise ValueError("tracking numbers cannot carry inferred outcomes")
    event = {"schema": EVIDENCE_SCHEMA, "event_id": str(uuid.uuid4()), "kind": "evidence",
             "record_id": record_id, "occurred_at": _now(), "writer": os.environ.get("SKAGENT", "unknown"),
             "node": socket.gethostname(), "evidence": dict(evidence)}
    return _append(root / "evidence.jsonl", event)


def supersede(root: Path, record_id: str, correction: dict[str, Any], *, supersedes: str) -> dict[str, Any]:
    """Append a correction; old records remain immutable and addressable."""
    if not supersedes or not isinstance(correction, dict):
        raise ValueError("supersedes event id and object correction are required")
    if _tracking_claim(correction):
        raise ValueError("tracking numbers cannot carry inferred outcomes")
    # Corrections may supersede either structural state or a prior evidence
    # assertion.  In both cases the original event remains immutable and the
    # correction is a new structural event that can be audited by event_id.
    prior = read(root / "records.jsonl") + read(root / "evidence.jsonl")
    target = next((event for event in prior if event.get("event_id") == supersedes), None)
    if target is None:
        raise ValueError("supersedes must reference an existing record or evidence event")
    if target.get("record_id") != record_id:
        raise ValueError("supersedes event belongs to a different record")
    return append_record(root, record_id, {"correction": dict(correction), "supersedes": supersedes})


def read(path: Path) -> list[dict[str, Any]]:
    """Read and validate JSONL, returning parsed objects."""
    if not path.exists():
        return []
    result = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if raw.strip():
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"non-object JSON at {path}:{number}")
            result.append(value)
    return result
