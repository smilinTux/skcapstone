"""Build a mediated Link observation feed without giving Link connector access.

This module is a producer-side boundary. It may invoke the read-only ``gh``
CLI when explicitly run, while the Link seat only reads the resulting JSON
feed. A lineage manifest is required for every PR that can become a healthy
Link observation. Incomplete data is written to a diagnostic path and never
replaces the last valid feed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .link_cycle import _digest
from .link_observation_feed import SCHEMA, _canonical_payload, _reviewer

LINEAGE_SCHEMA = "skfleet.link-lineage/v1"
DEFAULT_MAX_AGE = timedelta(minutes=15)
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class ProducerError(ValueError):
    """Input cannot safely produce a healthy Link feed."""


class ReadOnlyPRConnector(Protocol):
    def list_open(self, repository: str) -> Sequence[Mapping[str, Any]]:
        """Return raw read-only PR observations for one repository."""


class GhReadOnlyConnector:
    """Use ``gh api`` with fixed read-only GET arguments and no shell."""

    def list_open(self, repository: str) -> Sequence[Mapping[str, Any]]:
        if "/" not in repository or any(not part.strip() for part in repository.split("/", 1)):
            raise ProducerError("repository must be owner/name")
        command = [
            "gh",
            "api",
            "--paginate",
            f"repos/{repository}/pulls?state=open&per_page=100",
            "--jq",
            ".[]",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProducerError("connector_unavailable") from exc
        if completed.returncode != 0:
            raise ProducerError("connector_unavailable")
        rows: list[Mapping[str, Any]] = []
        for line in completed.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProducerError("connector_malformed") from exc
            if not isinstance(value, dict):
                raise ProducerError("connector_malformed")
            value["snapshot_at"] = _iso(_now())
            rows.append(value)
        return rows


@dataclass(frozen=True)
class ProducerIdentity:
    identity: str
    host: str
    session: str
    workspace: str

    def as_dict(self) -> dict[str, str]:
        return {
            "identity": self.identity,
            "host": self.host,
            "session": self.session,
            "workspace": self.workspace,
        }

    def validate(self) -> None:
        if not all(value.strip() for value in self.as_dict().values()):
            raise ProducerError("producer identity incomplete")


@dataclass(frozen=True)
class ProducerResult:
    healthy: bool
    reason: str
    records: int
    source_revision: str | None = None
    evidence_sha256: str | None = None
    blocked_path: Path | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProducerError("snapshot_timestamp_malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProducerError("snapshot_timestamp_malformed") from exc
    if parsed.tzinfo is None:
        raise ProducerError("snapshot_timestamp_malformed")
    return parsed.astimezone(timezone.utc)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ProducerError(f"{label}_malformed")
    return value


def load_lineage(path: Path) -> Mapping[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProducerError("lineage_unavailable") from exc
    data = _mapping(raw, "lineage")
    if data.get("schema") != LINEAGE_SCHEMA:
        raise ProducerError("lineage_schema_mismatch")
    records = data.get("records")
    reviewers = data.get("reviewer_candidates")
    if not isinstance(records, dict) or not isinstance(reviewers, list):
        raise ProducerError("lineage_malformed")
    try:
        for reviewer in reviewers:
            _reviewer(reviewer)
    except (TypeError, ValueError) as exc:
        raise ProducerError("lineage_reviewer_invalid") from exc
    return data


def _key(repository: str, number: int) -> str:
    return f"{repository}#{number}"


def _author(raw: Mapping[str, Any]) -> str:
    author = raw.get("author") or raw.get("user")
    if isinstance(author, dict):
        author = author.get("login") or author.get("name")
    return str(author or "")


def _sha(raw: Mapping[str, Any], direct: str, nested: str) -> str:
    value = raw.get(direct) or raw.get(f"{nested}_sha") or raw.get(nested)
    if isinstance(value, dict):
        value = value.get("sha")
    return str(value or "")


def _ci_state(raw: Mapping[str, Any]) -> str:
    checks = raw.get("statusCheckRollup") or raw.get("checks")
    if not isinstance(checks, list) or not checks:
        return "unknown"
    states = {
        str(item.get("conclusion") or item.get("status") or "").lower()
        for item in checks
        if isinstance(item, dict)
    }
    if states & {"failure", "failed", "cancelled", "timed_out", "error"}:
        return "failure"
    if states & {"queued", "in_progress", "pending", ""}:
        return "pending"
    return "success" if states <= {"success", "passed", "neutral", "skipped"} else "unknown"


def _conflict_state(raw: Mapping[str, Any]) -> str:
    mergeable = str(raw.get("mergeable") or "").lower()
    state = str(raw.get("mergeStateStatus") or raw.get("mergeable_state") or "").lower()
    if mergeable in {"false", "conflicting"} or state in {"dirty", "conflicting"}:
        return "conflict"
    if mergeable in {"true", "mergeable"} or state in {"clean", "unstable", "blocked"}:
        return "clean"
    return "unknown"


def _review_requests(raw: Mapping[str, Any]) -> list[str]:
    values = raw.get("reviewRequests") or raw.get("requested_reviewers") or []
    if not isinstance(values, list):
        raise ProducerError("review_requests_malformed")
    result: list[str] = []
    for value in values:
        item = value.get("login") if isinstance(value, dict) else value
        if not isinstance(item, str) or not item.strip():
            raise ProducerError("review_requests_malformed")
        result.append(item.strip())
    return result


def _lineage_record(lineage: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    records = lineage["records"]
    value = records.get(key)
    return value if isinstance(value, dict) else None


def _mapping_evidence(
    repository: str,
    number: int,
    head: str,
    base: str,
    source: str,
    generation: str,
    review: str,
    review_revision: str,
    review_verdict: str,
) -> str:
    value = {
        "repository": repository,
        "number": number,
        "head_revision": head,
        "base_revision": base,
        "source_card": source,
        "card_generation": generation,
        "review_card_id": review,
        "review_card_revision": review_revision,
        "review_verdict": review_verdict,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_feed(
    connector: ReadOnlyPRConnector,
    *,
    repositories: Sequence[str],
    lineage: Mapping[str, Any],
    producer: ProducerIdentity,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> tuple[dict[str, Any] | None, ProducerResult]:
    """Build a feed from complete lineage while reporting omitted PRs."""

    producer.validate()
    coverage = lineage.get("coverage")
    if not isinstance(coverage, dict):
        return None, ProducerResult(healthy=False, reason="lineage_incomplete", records=0)
    timestamp = now or _now()
    raw_rows: list[dict[str, Any]] = []
    for repository in repositories:
        try:
            rows = connector.list_open(repository)
        except ProducerError:
            raise
        for raw in rows:
            item = dict(_mapping(raw, "pr"))
            item["repository"] = repository
            raw_rows.append(item)

    keys: set[str] = set()
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw in raw_rows:
        snapshot_at = _parse_timestamp(raw.get("snapshot_at", _iso(timestamp)))
        if snapshot_at > timestamp + timedelta(seconds=30):
            raise ProducerError("snapshot_clock_ahead")
        if timestamp - snapshot_at > max_age:
            raise ProducerError("snapshot_stale")
        try:
            number = int(raw["number"])
            head = _sha(raw, "headRefOid", "head")
            base = _sha(raw, "baseRefOid", "base")
            title = str(raw.get("title") or "")
            author = _author(raw)
            key = _key(str(raw["repository"]), number)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProducerError("pr_malformed") from exc
        if key in keys:
            raise ProducerError("duplicate_pr")
        keys.add(key)
        if not _GIT_SHA.fullmatch(head) or not _GIT_SHA.fullmatch(base):
            raise ProducerError("pr_sha_malformed")
        metadata = _lineage_record(lineage, key)
        if metadata is None:
            missing.append(key)
            continue
        required = (
            "source_card",
            "card_generation",
            "review_card_id",
            "review_card_revision",
            "review_verdict",
            "head_revision",
            "base_revision",
            "mapping_evidence_sha256",
        )
        if any(not str(metadata.get(field) or "").strip() for field in required):
            missing.append(key)
            continue
        if metadata["head_revision"] != head or metadata["base_revision"] != base:
            missing.append(key)
            continue
        if not _GIT_SHA.fullmatch(str(metadata["head_revision"])) or not _GIT_SHA.fullmatch(
            str(metadata["base_revision"])
        ):
            missing.append(key)
            continue
        expected_mapping = _mapping_evidence(
            str(raw["repository"]),
            number,
            head,
            base,
            str(metadata["source_card"]),
            str(metadata["card_generation"]),
            str(metadata["review_card_id"]),
            str(metadata["review_card_revision"]),
            str(metadata["review_verdict"]),
        )
        if metadata["mapping_evidence_sha256"] != expected_mapping:
            missing.append(key)
            continue
        records.append(
            {
                "observation": {
                    "repository": str(raw["repository"]),
                    "number": number,
                    "title": title,
                    "author": author,
                    "head_sha": head,
                    "base_sha": base,
                    "ci_state": _ci_state(raw),
                    "conflict_state": _conflict_state(raw),
                    "review_requests": _review_requests(raw),
                    "source_card": str(metadata["source_card"]),
                    "card_generation": str(metadata["card_generation"]),
                    "observed_at": _iso(timestamp),
                    "active_review_card": metadata.get("active_review_card"),
                    "lineage_outcomes": (str(metadata["review_verdict"]),),
                },
                "review_card_id": str(metadata["review_card_id"]),
                "review_card_revision": str(metadata["review_card_revision"]),
                "review_verdict": str(metadata["review_verdict"]),
                "mapping_evidence_sha256": str(metadata["mapping_evidence_sha256"]),
            }
        )

    source_revision = _digest({"repositories": list(repositories), "pull_requests": raw_rows})
    if missing and not records:
        return None, ProducerResult(
            healthy=False,
            reason="lineage_incomplete",
            records=len(raw_rows),
            source_revision=source_revision,
        )
    payload = {
        "schema": SCHEMA,
        "source_revision": source_revision,
        "observed_at": _iso(timestamp),
        "producer": producer.as_dict(),
        "records": records,
        "reviewer_candidates": list(lineage["reviewer_candidates"]),
        "lineage_status": {
            "complete": len(records),
            "unresolved": len(missing),
            "unresolved_sha256": _digest(sorted(missing)),
        },
    }
    payload["evidence_sha256"] = _digest(_canonical_payload(payload))
    return payload, ProducerResult(
        healthy=True,
        reason="complete_with_unresolved" if missing else "complete",
        records=len(records),
        source_revision=source_revision,
        evidence_sha256=str(payload["evidence_sha256"]),
    )


def atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace one feed atomically, only after complete JSON serialization."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def produce(
    *,
    connector: ReadOnlyPRConnector,
    repositories: Sequence[str],
    lineage_path: Path,
    output_path: Path,
    producer: ProducerIdentity,
    now: datetime | None = None,
) -> ProducerResult:
    """Build and atomically publish only a complete healthy feed."""

    lineage = load_lineage(lineage_path)
    payload, result = build_feed(
        connector,
        repositories=repositories,
        lineage=lineage,
        producer=producer,
        now=now,
    )
    if payload is not None:
        atomic_write(output_path, payload)
        return result
    blocked = output_path.with_name(output_path.name + ".blocked.json")
    atomic_write(
        blocked,
        {
            "schema": "skfleet.link-observation-producer-blocked/v1",
            "reason": result.reason,
            "records": result.records,
            "source_revision": result.source_revision,
            "at": _iso(now or _now()),
        },
    )
    return ProducerResult(**{**result.__dict__, "blocked_path": blocked})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", required=True)
    parser.add_argument("--lineage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--producer-identity", default=os.environ.get("SKAGENT", "link-producer"))
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument("--session", default=os.environ.get("SKSESSION", "manual"))
    parser.add_argument("--workspace", default=os.getcwd())
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    producer = ProducerIdentity(args.producer_identity, args.host, args.session, args.workspace)
    try:
        connector = GhReadOnlyConnector()
        lineage = load_lineage(args.lineage)
        if args.dry_run:
            _, result = build_feed(
                connector,
                repositories=args.repo,
                lineage=lineage,
                producer=producer,
            )
        else:
            result = produce(
                connector=connector,
                repositories=args.repo,
                lineage_path=args.lineage,
                output_path=args.output,
                producer=producer,
            )
    except ProducerError as exc:
        print(json.dumps({"healthy": False, "reason": str(exc)}, sort_keys=True))
        return 78
    print(json.dumps(result.__dict__, default=str, sort_keys=True))
    return 0 if result.healthy else 78


if __name__ == "__main__":
    raise SystemExit(main())
