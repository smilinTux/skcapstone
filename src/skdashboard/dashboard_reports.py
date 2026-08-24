"""Immutable control-plane report snapshots and read-only comparisons."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

REPORT_TYPES = frozenset(
    {
        "daily_operations",
        "weekly_portfolio",
        "sprint_flow",
        "monthly_service",
        "monthly_ai_economy",
        "quarterly_strategy",
        "ad_hoc_evidence",
    }
)
TRUTH_STATES = frozenset(
    {"current", "stale", "partial", "unavailable", "unreachable", "unknown", "not_applicable"}
)
REPORT_QUALITY_STATES = frozenset(TRUTH_STATES - {"not_applicable"})
MEASUREMENT_KINDS = frozenset({"measured", "derived", "estimated", "forecast"})
_STATE_ORDER = {
    "unavailable": 0,
    "unreachable": 1,
    "unknown": 2,
    "partial": 3,
    "stale": 4,
    "not_applicable": 5,
    "current": 6,
}
_SNAPSHOT_RE = re.compile(r"^rpt-[a-z0-9][a-z0-9-]{7,95}$")
_HASH_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
MAX_SNAPSHOTS = 500
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


class ReportSnapshotError(ValueError):
    """Raised when immutable report evidence is invalid or conflicts."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ReportSnapshotError("report content must be canonical JSON") from error


def _hash(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value)).hexdigest()}"


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ReportSnapshotError(f"{field} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReportSnapshotError(f"{field} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ReportSnapshotError(f"{field} requires an explicit timezone")
    return value


def report_hash(snapshot: Mapping[str, object]) -> str:
    """Hash the exact report body except its self-describing hash field."""
    return _hash({key: value for key, value in snapshot.items() if key != "report_hash"})


def _metric_identity(metric: Mapping[str, object]) -> str:
    metric_id = metric.get("metric_id")
    version = metric.get("definition_version")
    if not isinstance(metric_id, str) or not isinstance(version, str):
        raise ReportSnapshotError("metric identity is incomplete")
    return f"{metric_id}@{version}"


def _validate_metric(metric: object) -> dict:
    if not isinstance(metric, dict) or metric.get("schema_version") != "1.1.0":
        raise ReportSnapshotError("metric result schema is incompatible")
    identity = _metric_identity(metric)
    truth = metric.get("truth_state")
    kind = metric.get("measurement_kind")
    calculation = metric.get("calculation")
    source = metric.get("source")
    quality = metric.get("data_quality")
    if truth not in TRUTH_STATES or kind not in MEASUREMENT_KINDS:
        raise ReportSnapshotError(f"metric {identity} has invalid truth or measurement kind")
    if not isinstance(calculation, dict) or not _HASH_RE.fullmatch(
        str(calculation.get("definition_hash", ""))
    ):
        raise ReportSnapshotError(f"metric {identity} has no definition hash")
    if not isinstance(calculation.get("method"), str) or not calculation["method"]:
        raise ReportSnapshotError(f"metric {identity} has no calculation method")
    if not isinstance(source, dict) or not isinstance(source.get("watermarks"), list):
        raise ReportSnapshotError(f"metric {identity} has no source watermark contract")
    if (
        not isinstance(quality, dict)
        or not isinstance(quality.get("errors"), list)
        or not isinstance(quality.get("exclusions"), list)
    ):
        raise ReportSnapshotError(f"metric {identity} has no data-quality contract")
    if truth in {"current", "stale", "partial"} and metric.get("value") is not None:
        if not source["watermarks"]:
            raise ReportSnapshotError(f"metric {identity} has a value without a watermark")
        numerator = metric.get("numerator")
        denominator = metric.get("denominator")
        method = calculation["method"]
        expected = numerator
        if method in {"ratio", "ratio_percent"}:
            if (
                not isinstance(numerator, (int, float))
                or isinstance(numerator, bool)
                or not isinstance(denominator, (int, float))
                or isinstance(denominator, bool)
                or denominator <= 0
            ):
                raise ReportSnapshotError(f"metric {identity} has invalid calculation inputs")
            expected = numerator / denominator
            if method == "ratio_percent":
                expected *= 100
        if method not in {"count", "ratio", "ratio_percent"} or metric["value"] != round(
            expected, 6
        ):
            raise ReportSnapshotError(
                f"metric {identity} value does not reproduce its calculation"
            )
    if (
        truth in {"unavailable", "unreachable", "unknown", "not_applicable"}
        and metric.get("value") is not None
    ):
        raise ReportSnapshotError(f"metric {identity} has a value in a no-value truth state")
    return dict(metric)


def _quality(metrics: list[dict]) -> dict:
    truths = Counter(str(metric["truth_state"]) for metric in metrics)
    kinds = Counter(str(metric["measurement_kind"]) for metric in metrics)
    errors = sorted(
        {
            str(value)[:256]
            for metric in metrics
            for value in metric["data_quality"]["errors"]
            if isinstance(value, str) and value
        }
    )[:128]
    exclusions = sorted(
        {
            str(value)[:256]
            for metric in metrics
            for value in metric["data_quality"]["exclusions"]
            if isinstance(value, str) and value
        }
    )[:128]
    observed_truths = [state for state in truths if state != "not_applicable"]
    truth_state = min(observed_truths, key=_STATE_ORDER.get) if observed_truths else "unknown"
    summary = "; ".join(
        [
            *(f"{truths[state]} {state}" for state in sorted(truths)),
            *(f"{kinds[kind]} {kind}" for kind in sorted(kinds)),
        ]
    )
    return {
        "truth_state": truth_state,
        "visibility": {"state": "visible", "authorization": "authorized"},
        "summary": summary[:1000],
        "errors": errors,
        "exclusions": exclusions,
    }


def _watermarks(metrics: Iterable[dict]) -> list[dict]:
    values = {
        (item.get("source"), item.get("value"))
        for metric in metrics
        for item in metric["source"]["watermarks"]
        if isinstance(item, dict)
        and isinstance(item.get("source"), str)
        and item.get("source")
        and isinstance(item.get("value"), str)
        and item.get("value")
    }
    if not values:
        raise ReportSnapshotError("a report requires at least one source watermark")
    return [{"source": source, "value": value} for source, value in sorted(values)]


def _model_provenance(insights: Iterable[dict]) -> list[dict]:
    values = []
    seen = set()
    for insight in insights:
        provenance = insight.get("model_provenance")
        if not isinstance(provenance, dict):
            raise ReportSnapshotError("AI narrative requires typed model provenance")
        marker = _canonical(provenance)
        if marker not in seen:
            values.append(dict(provenance))
            seen.add(marker)
    return values


def build_report_snapshot(
    *,
    report_type: str,
    audience: list[str],
    generated_at: str,
    as_of: str,
    scope: dict,
    baseline: str | None,
    sections: list[dict],
    review_state: dict | None = None,
    supersedes: str | None = None,
) -> dict:
    """Build one deterministic schema-compatible snapshot without storing it."""
    if report_type not in REPORT_TYPES:
        raise ReportSnapshotError("report type is unsupported")
    if (
        not isinstance(audience, list)
        or not 1 <= len(audience) <= 16
        or not all(isinstance(value, str) and 0 < len(value) <= 128 for value in audience)
    ):
        raise ReportSnapshotError("report audience is invalid")
    _timestamp(generated_at, "generated_at")
    _timestamp(as_of, "as_of")
    if (
        not isinstance(scope, dict)
        or not scope
        or any(
            key in scope for key in ("tenant_id", "matter_id", "person_id", "user_id", "agent_id")
        )
    ):
        raise ReportSnapshotError("report scope is empty or protected")
    if baseline is not None and (not isinstance(baseline, str) or len(baseline) > 128):
        raise ReportSnapshotError("report baseline is invalid")
    if supersedes is not None and not _SNAPSHOT_RE.fullmatch(supersedes):
        raise ReportSnapshotError("superseded snapshot id is invalid")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 32:
        raise ReportSnapshotError("a report requires bounded sections")

    safe_sections = []
    all_metrics = []
    all_insights = []
    for section in sections:
        if not isinstance(section, dict) or set(section) != {
            "section_id",
            "title",
            "metric_results",
            "insights",
        }:
            raise ReportSnapshotError("report section contract is invalid")
        section_id = section["section_id"]
        title = section["title"]
        metrics = section["metric_results"]
        insights = section["insights"]
        if not isinstance(section_id, str) or not section_id or len(section_id) > 128:
            raise ReportSnapshotError("report section id is invalid")
        if not isinstance(title, str) or len(title) > 200:
            raise ReportSnapshotError("report section title is invalid")
        if not isinstance(metrics, list) or len(metrics) > 256:
            raise ReportSnapshotError("report section metrics are invalid")
        if (
            not isinstance(insights, list)
            or len(insights) > 64
            or not all(isinstance(value, dict) for value in insights)
        ):
            raise ReportSnapshotError("report section insights are invalid")
        validated_metrics = [_validate_metric(metric) for metric in metrics]
        safe_sections.append(
            {
                "section_id": section_id,
                "title": title,
                "metric_results": validated_metrics,
                "insights": [dict(value) for value in insights],
            }
        )
        all_metrics.extend(validated_metrics)
        all_insights.extend(insights)
    if not all_metrics:
        raise ReportSnapshotError("a report requires at least one metric result")

    definition_hashes = {}
    for metric in all_metrics:
        identity = _metric_identity(metric)
        definition_hash = metric["calculation"]["definition_hash"]
        previous = definition_hashes.setdefault(identity, definition_hash)
        if previous != definition_hash:
            raise ReportSnapshotError(f"metric {identity} has conflicting definition hashes")
    model_provenance = _model_provenance(all_insights)
    review = dict(review_state or {"state": "unreviewed"})
    if review.get("state") not in {"unreviewed", "reviewed", "approved", "rejected"}:
        raise ReportSnapshotError("report review state is invalid")

    identity_body = {
        "report_type": report_type,
        "audience": audience,
        "generated_at": generated_at,
        "as_of": as_of,
        "scope": scope,
        "baseline": baseline,
        "metric_definition_hashes": definition_hashes,
        "source_watermarks": _watermarks(all_metrics),
        "quality_statement": _quality(all_metrics),
        "sections": safe_sections,
        "review_state": review,
        "supersedes": supersedes,
    }
    if model_provenance:
        identity_body["model_provenance"] = model_provenance
    snapshot_id = f"rpt-{hashlib.sha256(_canonical(identity_body)).hexdigest()[:24]}"
    snapshot = {
        "snapshot_id": snapshot_id,
        "schema_version": "1.1.0",
        **identity_body,
    }
    snapshot["report_hash"] = report_hash(snapshot)
    validate_report_snapshot(snapshot)
    return snapshot


def validate_report_snapshot(snapshot: object) -> dict:
    """Validate immutable, reproducible invariants at the storage and read boundary."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "1.1.0":
        raise ReportSnapshotError("report snapshot schema is incompatible")
    snapshot_id = snapshot.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_RE.fullmatch(snapshot_id):
        raise ReportSnapshotError("report snapshot id is invalid")
    if snapshot.get("report_type") not in REPORT_TYPES:
        raise ReportSnapshotError("report snapshot type is invalid")
    scope = snapshot.get("scope")
    if (
        not isinstance(scope, dict)
        or not scope
        or any(
            key in scope for key in ("tenant_id", "matter_id", "person_id", "user_id", "agent_id")
        )
    ):
        raise ReportSnapshotError("report scope is empty or protected")
    if snapshot.get("report_hash") != report_hash(snapshot):
        raise ReportSnapshotError("report snapshot hash does not match its content")
    identity_body = {
        key: value
        for key, value in snapshot.items()
        if key not in {"snapshot_id", "schema_version", "report_hash"}
    }
    expected_id = f"rpt-{hashlib.sha256(_canonical(identity_body)).hexdigest()[:24]}"
    if snapshot_id != expected_id:
        raise ReportSnapshotError("report snapshot id is not content addressed")
    if snapshot.get("supersedes") == snapshot_id:
        raise ReportSnapshotError("a report cannot supersede itself")
    sections = snapshot.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ReportSnapshotError("report snapshot has no sections")
    metrics = [
        _validate_metric(metric)
        for section in sections
        if isinstance(section, dict)
        for metric in section.get("metric_results", [])
    ]
    insights = [
        insight
        for section in sections
        if isinstance(section, dict)
        for insight in section.get("insights", [])
        if isinstance(insight, dict)
    ]
    expected_provenance = _model_provenance(insights)
    if snapshot.get("model_provenance", []) != expected_provenance:
        raise ReportSnapshotError("report model provenance does not reproduce insight content")
    expected_hashes = {
        _metric_identity(metric): metric["calculation"]["definition_hash"] for metric in metrics
    }
    if snapshot.get("metric_definition_hashes") != expected_hashes:
        raise ReportSnapshotError("report definition hashes do not reproduce metric content")
    expected_watermarks = _watermarks(metrics)
    if snapshot.get("source_watermarks") != expected_watermarks:
        raise ReportSnapshotError("report source watermarks do not reproduce metric content")
    expected_quality = _quality(metrics)
    if snapshot.get("quality_statement") != expected_quality:
        raise ReportSnapshotError("report quality statement does not reproduce metric content")
    _timestamp(snapshot.get("generated_at"), "generated_at")
    _timestamp(snapshot.get("as_of"), "as_of")
    return dict(snapshot)


def compare_report_snapshots(current: dict, baseline: dict) -> dict:
    """Compare frozen values without harmonizing definitions or truth states."""
    validate_report_snapshot(current)
    validate_report_snapshot(baseline)
    if current["scope"] != baseline["scope"]:
        return {
            "state": "incompatible_scope",
            "current_snapshot_id": current["snapshot_id"],
            "baseline_snapshot_id": baseline["snapshot_id"],
            "metric_changes": [],
            "watermark_changed": None,
        }
    current_metrics = {
        _metric_identity(metric): metric
        for section in current["sections"]
        for metric in section["metric_results"]
    }
    baseline_metrics = {
        _metric_identity(metric): metric
        for section in baseline["sections"]
        for metric in section["metric_results"]
    }
    changes = []
    for identity in sorted(set(current_metrics) | set(baseline_metrics)):
        newer = current_metrics.get(identity)
        older = baseline_metrics.get(identity)
        comparable = bool(
            newer
            and older
            and newer["calculation"]["definition_hash"] == older["calculation"]["definition_hash"]
            and newer["truth_state"] in {"current", "stale", "partial"}
            and older["truth_state"] in {"current", "stale", "partial"}
            and isinstance(newer.get("value"), (int, float))
            and not isinstance(newer.get("value"), bool)
            and isinstance(older.get("value"), (int, float))
            and not isinstance(older.get("value"), bool)
        )
        changes.append(
            {
                "metric_ref": identity,
                "current_value": newer.get("value") if newer else None,
                "baseline_value": older.get("value") if older else None,
                "current_truth_state": newer.get("truth_state") if newer else "unknown",
                "baseline_truth_state": older.get("truth_state") if older else "unknown",
                "definition_changed": bool(
                    newer
                    and older
                    and newer["calculation"]["definition_hash"]
                    != older["calculation"]["definition_hash"]
                ),
                "comparable": comparable,
                "delta": newer["value"] - older["value"] if comparable else None,
            }
        )
    return {
        "state": "comparable",
        "current_snapshot_id": current["snapshot_id"],
        "baseline_snapshot_id": baseline["snapshot_id"],
        "metric_changes": changes,
        "watermark_changed": current["source_watermarks"] != baseline["source_watermarks"],
    }


class ReportSnapshotStore:
    """Small immutable local store for offline-created report snapshots."""

    def __init__(self, home: Path):
        self.root = Path(home).expanduser() / "reports" / "snapshots"

    def _path(self, snapshot_id: str) -> Path:
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_RE.fullmatch(snapshot_id):
            raise ReportSnapshotError("report snapshot id is invalid")
        return self.root / f"{snapshot_id}.json"

    def put(self, snapshot: dict) -> dict:
        validated = validate_report_snapshot(snapshot)
        supersedes = validated.get("supersedes")
        if supersedes is not None:
            self.get(supersedes)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ReportSnapshotError("report snapshot directory is unsafe")
        path = self._path(validated["snapshot_id"])
        content = _canonical(validated) + b"\n"
        if len(content) > MAX_SNAPSHOT_BYTES:
            raise ReportSnapshotError("report snapshot exceeds its storage budget")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            existing = self.get(validated["snapshot_id"])
            if _canonical(existing) != _canonical(validated):
                raise ReportSnapshotError("immutable report snapshot id conflicts")
            return existing
        try:
            written = 0
            while written < len(content):
                written += os.write(descriptor, content[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return validated

    def get(self, snapshot_id: str) -> dict:
        path = self._path(snapshot_id)
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise KeyError(snapshot_id) from error
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ReportSnapshotError("report snapshot source is unsafe")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ReportSnapshotError("report snapshot source is unsafe")
            content = b""
            while len(content) <= MAX_SNAPSHOT_BYTES:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                content += chunk
            if len(content) > MAX_SNAPSHOT_BYTES:
                raise ReportSnapshotError("report snapshot exceeds its read budget")
        finally:
            os.close(descriptor)
        try:
            raw = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ReportSnapshotError("report snapshot source is malformed") from error
        validated = validate_report_snapshot(raw)
        if validated["snapshot_id"] != snapshot_id:
            raise ReportSnapshotError("report snapshot path and content disagree")
        return validated

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        if self.root.is_symlink() or not self.root.is_dir():
            raise ReportSnapshotError("report snapshot directory is unsafe")
        names = sorted(
            path.stem
            for path in self.root.iterdir()
            if path.name.endswith(".json") and _SNAPSHOT_RE.fullmatch(path.stem)
        )
        if len(names) > MAX_SNAPSHOTS:
            raise ReportSnapshotError("report snapshot population exceeds its read budget")
        return [self.get(snapshot_id) for snapshot_id in names]


def _summary(snapshot: dict) -> dict:
    metric_count = sum(len(section["metric_results"]) for section in snapshot["sections"])
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "report_type": snapshot["report_type"],
        "generated_at": snapshot["generated_at"],
        "as_of": snapshot["as_of"],
        "scope": snapshot["scope"],
        "baseline": snapshot.get("baseline"),
        "truth_state": snapshot["quality_statement"]["truth_state"],
        "quality_summary": snapshot["quality_statement"]["summary"],
        "metric_count": metric_count,
        "review_state": snapshot["review_state"],
        "supersedes": snapshot.get("supersedes"),
        "report_hash": snapshot["report_hash"],
    }


def get_report_projection(
    home: Path,
    query: dict,
    *,
    store_factory=ReportSnapshotStore,
) -> dict:
    """Return bounded report summaries, one selected snapshot, and optional comparison."""
    store = store_factory(home)
    snapshots = sorted(
        store.list(), key=lambda item: (item["generated_at"], item["snapshot_id"]), reverse=True
    )
    report_type = query.get("report_type", "all")
    visible = [
        item for item in snapshots if report_type == "all" or item["report_type"] == report_type
    ]
    selected_id = query.get("snapshot")
    selected = store.get(selected_id) if selected_id else (visible[0] if visible else None)
    baseline_id = query.get("compare")
    baseline = store.get(baseline_id) if baseline_id else None
    comparison = compare_report_snapshots(selected, baseline) if selected and baseline else None
    return {
        "schema_version": "1.0.0",
        "projection_id": "reports-latest",
        "scope": {
            "role": query["role"],
            "scope": query["scope"],
            "window": query["window"],
            "baseline": query["baseline"],
            "service": query["service"],
        },
        "truth_state": "current" if visible else "unknown",
        "reports": [_summary(item) for item in visible],
        "selected": selected,
        "comparison": comparison,
        "store": {
            "mode": "immutable_read_only_projection",
            "http_creation_authorized": False,
            "http_correction_authorized": False,
            "export_authorized": False,
        },
        "errors": []
        if visible
        else [
            {"code": "REPORTS_UNKNOWN", "message": "No immutable report snapshot is available."}
        ],
    }


class ReportProjectionProvider:
    """Read immutable snapshots only while the exact CapAuth decision remains current."""

    def __init__(self, *, store_factory=ReportSnapshotStore):
        self.store_factory = store_factory

    def read(self, context, query, home, *, currentness_verifier):
        if currentness_verifier.check_before_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision is not current")
        projection = get_report_projection(home, query, store_factory=self.store_factory)
        if currentness_verifier.check_after_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision expired during owner read")
        return projection

    def read_snapshot(self, context, snapshot_id, home, *, currentness_verifier):
        if currentness_verifier.check_before_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision is not current")
        snapshot = self.store_factory(home).get(snapshot_id)
        if currentness_verifier.check_after_owner_read(context).value != "allow":
            raise PermissionError("control-plane decision expired during owner read")
        return snapshot
