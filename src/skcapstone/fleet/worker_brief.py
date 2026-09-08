"""Deterministic, non-secret evidence materialization for fleet worker briefs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SENSITIVE_RE = re.compile(r"(?i)(?:credential|password|secret|token|authorization)")
SAFE_LINK_KEYS = {
    "artifact_sha256",
    "artifacts_sha256",
    "candidate_commit",
    "candidate_evidence_sha256",
    "candidate_patch_sha256",
    "candidate_tree",
    "evidence",
    "evidence_path",
    "evidence_sha256",
    "incident_receipt",
    "open_pr",
    "pr",
    "producer_identity",
    "pull_request",
    "repository_scope",
    "source_manifest_sha256",
    "source_scope",
    "supersedes",
}
DEFAULT_PROHIBITED_ACTIONS = (
    "capability-token-disclosure",
    "credential-access",
    "database-mutation",
    "deployment",
    "protected-content-access",
    "service-change",
    "unrestricted-tool-grant",
)


class BriefEvidenceError(ValueError):
    """Raised when an immutable artifact required by a brief is unavailable."""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__(json.dumps(report, sort_keys=True, separators=(",", ":")))
        self.report = report


def safe_brief_links(links: object) -> dict[str, str]:
    """Return only bounded evidence metadata approved for worker prompts."""
    if not isinstance(links, dict):
        return {}
    safe: dict[str, str] = {}
    for raw_key, raw_value in links.items():
        key = str(raw_key).strip()
        value = str(raw_value).strip()
        if not key or not value or SENSITIVE_RE.search(key):
            continue
        is_hash = key.endswith("_sha256") or key.endswith("_commit") or key.endswith("_tree")
        if key not in SAFE_LINK_KEYS and not is_hash:
            continue
        if SENSITIVE_RE.search(value):
            continue
        safe[key] = value
    return dict(sorted(safe.items()))


def _artifact_paths(evidence_root: Path, parent_id: str, digests: set[str]) -> dict[str, str]:
    """Resolve required hashes to exact files inside one bounded producer folder."""
    found: dict[str, str] = {}
    parent = evidence_root / "work" / parent_id
    if not parent.is_dir():
        return found
    for path in sorted(parent.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in digests and digest not in found:
            found[digest] = str(path)
        if len(found) == len(digests):
            break
    return found


def write_missing_report(report: dict[str, Any], evidence_root: Path) -> tuple[Path, str]:
    """Write one content-addressed immutable pre-claim failure report."""
    body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    payload = dict(report, content_sha256=digest)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    directory = evidence_root / "worker-brief-preflight"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report['card_id']}-{digest}.json"
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise ValueError("immutable worker brief report collision")
    return path, digest


def materialize_work_envelope(
    card: dict[str, Any],
    *,
    labels: list[str],
    lane: str,
    model: str,
    seat: str | None,
    evidence_root: Path,
) -> dict[str, Any]:
    """Build and validate the exact non-secret card payload used by a worker."""
    card_id = str(card.get("id") or "")
    links = safe_brief_links(card.get("links"))
    parents = sorted(label[7:] for label in labels if re.fullmatch(r"parent-[0-9a-f]{8}", label))
    required_digests = {
        value.lower()
        for key, value in links.items()
        if key.startswith("candidate_")
        and key.endswith("_sha256")
        and SHA256_RE.fullmatch(value.lower())
    }
    evidence_paths: dict[str, str] = {}
    if required_digests and len(parents) == 1:
        evidence_paths = _artifact_paths(evidence_root, parents[0], required_digests)
    missing = sorted(required_digests - set(evidence_paths))
    if missing:
        raise BriefEvidenceError(
            {
                "card_id": card_id,
                "kind": "worker-brief-evidence-missing",
                "missing_paths": [
                    {
                        "expected_root": str(
                            evidence_root
                            / "work"
                            / (parents[0] if len(parents) == 1 else "<single-parent-required>")
                        ),
                        "sha256": digest,
                    }
                    for digest in missing
                ],
            }
        )
    envelope = {
        "acceptance_criteria": [str(value) for value in card.get("acceptance_criteria") or []],
        "card_id": card_id,
        "dependencies": [str(value) for value in card.get("dependencies") or []],
        "description": str(card.get("description") or ""),
        "evidence_links": links,
        "evidence_paths": evidence_paths,
        "kind": str(card.get("kind") or "task"),
        "model_policy": {
            "lane": lane,
            "model": model,
            "qwen_first_exclusive": "qwen-first-exclusive" in labels,
            "seat": seat,
        },
        "producer_identity": links.get("producer_identity") or str(card.get("created_by") or ""),
        "prohibited_actions": list(DEFAULT_PROHIBITED_ACTIONS),
        "route_and_seat_labels": sorted(labels),
        "source_scope": [
            links[key] for key in ("repository_scope", "source_scope") if key in links
        ],
        "title": str(card.get("title") or ""),
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    if SENSITIVE_RE.search(encoded) and any(
        marker in encoded.lower()
        for marker in ("authorization:", "password=", "secret=", "token=")
    ):
        raise ValueError("worker brief contains credential-shaped content")
    return envelope


def format_work_envelope(envelope: dict[str, Any]) -> str:
    """Return stable bytes for inclusion in a worker prompt."""
    return json.dumps(envelope, indent=2, sort_keys=True) + "\n"
