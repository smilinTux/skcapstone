"""Immutable prelaunch gateway observations for native claim-bound receipts."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from ..fleet_lane_health import MAX_AGE_SECONDS
from .production_dispatch import resolve_production_routes
from .review_capacity import _review_capacity_truth_is_current

_BOUND = 4 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_SIZES = {"S": "sk-s", "M": "sk-m", "L": "sk-l", "XL": "sk-xl"}


def card_required_size(card):
    """Match native dispatch: one title marker, otherwise one canonical label."""
    title = re.findall(r"\[(S|M|L|XL)\]", str(getattr(card, "title", "")))
    if len(title) == 1:
        return title[0]
    labels = {str(label).strip().lower() for label in getattr(card, "labels", ())}
    sizes = {size for size, route in _SIZES.items() if route in labels}
    return next(iter(sizes)) if len(sizes) == 1 else None


def _valid_snapshot(snapshot):
    return (
        isinstance(snapshot, dict)
        and snapshot.get("schema_version") == 1
        and snapshot.get("error") is None
        and isinstance(snapshot.get("routes"), list)
        and type(snapshot.get("observed_at")) in (int, float)
        and _review_capacity_truth_is_current(snapshot)
    )


def load_production_snapshot(home: Path, reference: dict) -> dict:
    """Read only the exact private content-addressed observation in this home."""
    if (
        not isinstance(reference, dict)
        or set(reference) != {"path", "sha256", "capacity_revision"}
        or any(
            not isinstance(reference[key], str) or not _SHA.fullmatch(reference[key])
            for key in ("sha256", "capacity_revision")
        )
    ):
        raise ValueError("production snapshot reference is invalid")
    path = Path(home) / "evidence/production-routes" / (reference["capacity_revision"] + ".json")
    if str(path) != reference["path"] or path.parent.is_symlink():
        raise ValueError("production snapshot path is invalid")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("production snapshot is not a private owned regular file")
        raw = stream.read(_BOUND + 1)
    if len(raw) > _BOUND or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("production snapshot content changed")
    snapshot = json.loads(raw)
    if (
        not _valid_snapshot(snapshot)
        or snapshot["capacity_revision"] != reference["capacity_revision"]
    ):
        raise ValueError("production snapshot seal changed")
    return snapshot


def persist_production_snapshot(home: Path, snapshot: dict) -> dict:
    """Atomically publish once by sealed revision; an existing file must match."""
    if (
        not _valid_snapshot(snapshot)
        or not 0 <= time.time() - snapshot["observed_at"] <= MAX_AGE_SECONDS
    ):
        raise ValueError("production snapshot is not fresh and sealed")
    raw = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > _BOUND:
        raise ValueError("production snapshot exceeds bound")
    directory = Path(home) / "evidence/production-routes"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("production snapshot directory is redirected")
    path = directory / (snapshot["capacity_revision"] + ".json")
    reference = {
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "capacity_revision": snapshot["capacity_revision"],
    }
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        if load_production_snapshot(home, reference) != snapshot:
            raise ValueError("production snapshot collision")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return reference


def production_receipt_allowed(home: Path, policy: dict, launch: dict, card, event: dict) -> bool:
    """Replay prelaunch selection, preserving correctness when capacity changes."""
    try:
        route = event["route_identity"]
        remote = event.get("schema") == "skfleet.review-assignment-launch/v3"
        reference = route["production_snapshot"]
        if remote:
            from .review_dispatch import validate_execution

            reference = validate_execution(home, policy, launch, card, event)
        snapshot = load_production_snapshot(home, reference)
        stamp = datetime.datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
        size = card_required_size(card)
        if (
            stamp.tzinfo is None
            or not 0 <= stamp.timestamp() - snapshot["observed_at"] <= MAX_AGE_SECONDS
            or snapshot["endpoint"].rstrip("/") != policy["gateway_url"].rstrip("/")
            or (not remote and launch["host"] != policy["authority_host"])
            or event.get("writer") != launch["owner"]
            or event.get("claim_revision") != launch["revision"]
            or route.get("provider") != "skgateway"
            or route.get("model_or_bucket") != launch["model"]
            or route.get("logical_route") != _SIZES.get(size)
            or not isinstance(route.get("capacity_domains"), list)
            or len(route["capacity_domains"]) != 1
        ):
            return False
        eligible = resolve_production_routes(
            snapshot["routes"],
            policy=policy,
            required_size=size,
            labels=getattr(card, "labels", ()),
            lane=launch["lane"],
        )
        return any(
            row["model_or_bucket"] == launch["model"]
            and row["capacity_domain"] == route["capacity_domains"][0]
            for row in eligible
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def validate_review_execution(home, policy, launch, card, event, *, recorded=True):
    """Verify native offer, recommendation, claim, admission and unit provenance."""
    from skcoord.card_store import CardStore

    from . import builder_dispatch as dispatch
    from . import production_builder as production
    from .paths import FleetPaths
    from .production_admission import MARKER, _digest, _intent, _reservation_id
    from .production_review_finish import read_json
    from .review_dispatch import _recorded, _source, validate_contract
    from .source_bundle import _once

    execution = event["execution"]
    node = execution["node"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", node) or ".." in node:
        raise ValueError("remote execution node invalid")
    paths = FleetPaths(Path(home) / "fleet")
    request = read_json(dispatch.request_path(paths, node, card.id))
    validate_contract(request, policy, host=launch["host"], historical=True)
    _recorded(home, request)
    status = dispatch._validated_status(dispatch.status_path(paths, node, card.id), paths, node)
    if status is None:
        raise ValueError("native destination custody missing")
    bound = production.node_binding(paths, node, request["policy"])
    expected = dict(
        request_id=request["request_id"],
        request_sha256=production.digest(request),
        authority=policy["authority_host"],
        node=node,
        host=bound["host"],
        policy_sha256=request["production"]["policy_sha256"],
        work_kind="review",
        unit="skfleet-worker-" + request["production"]["family"] + "-" + card.id + ".service",
    )
    claim_events = [
        row
        for row in CardStore(home)._read_events(card.id)
        if row.get("action") == "claim"
        and row.get("writer") == launch["owner"]
        and (row.get("claim_revision") or row.get("event_id")) == launch["revision"]
    ]
    source = _source(home, card, historical=True)
    expected_source = dict(request["source"])
    observed_source = dict(source)
    # A release changes the live card revision. The offer's immutable ledger
    # event binds the source revision; content, producer claim, and bundle are
    # revalidated against the sealed manifest and PASS_FOR_REVIEW event.
    expected_source.pop("revision", None)
    observed_source.pop("revision", None)
    if (
        event.get("schema") != "skfleet.review-assignment-launch/v3"
        or event.get("action") != "review_assignment_launch"
        or event.get("launched") is not True
        or not {"review", "seat-seraph", "source-only"} <= set(card.labels)
        or any(execution.get(k) != v for k, v in expected.items())
        or execution != status.get("execution")
        or launch["host"] != execution["host"]
        or launch["owner"] != request["reviewer"]
        or len(claim_events) != 1
        or card.meta.get("claim_conflicts")
        or (card.owner is not None and launch["owner"] != card.owner)
        or (card.owner is not None and launch["revision"] != card.meta.get("_claim_revision"))
        or event.get("writer") != launch["owner"]
        or event.get("reviewer") != launch["owner"]
        or event.get("claim_revision") != launch["revision"]
        or event.get("observed_state_revision") != request["review_revision"]
        or status.get("request_id") != request["request_id"]
        or status.get("owner") != launch["owner"]
        or status.get("claim_revision") != launch["revision"]
        or status.get("writer", {}).get("role") != "sknoded"
        or status.get("writer", {}).get("node") != node
        or not status.get("writer", {}).get("identity")
        or observed_source != expected_source
    ):
        raise ValueError("remote execution provenance differs")
    binding = dict(
        card_id=card.id,
        owner=launch["owner"],
        claim_revision=launch["revision"],
        request_id=request["request_id"],
        request_sha256=production.digest(request),
        policy_sha256=request["production"]["policy_sha256"],
        work_kind="review",
    )
    intent = _intent(
        request["policy"], execution["host"], execution["unit"], binding, status["command"]
    )
    ack = status["acknowledgment"]
    if (
        status["admission"] != intent
        or execution["admission_id"] != _reservation_id(intent)
        or execution["admission_sha256"] != _digest(intent)
        or not re.fullmatch(r"[0-9a-f]{32}", execution["invocation"])
        or ack.get("Id") != execution["unit"]
        or ack.get("LoadState") != "loaded"
        or ack.get("InvocationID") != execution["invocation"]
        or ack.get(MARKER) != execution["admission_id"]
        or int(ack.get("MemoryMax", "0")) != request["production"]["resources"]["memory_max_bytes"]
    ):
        raise ValueError("remote service acknowledgment or admission differs")
    events = CardStore(home)._read_events(card.id)
    recommendations = [
        e
        for e in events
        if e.get("action") == "review_assignment_recommendation"
        and e.get("recommendation_id") == event.get("recommendation_id")
    ]
    if (
        len(recommendations) != 1
        or recommendations[0].get("writer") != "link"
        or recommendations[0].get("reviewer") != request["reviewer"]
        or recommendations[0].get("author") != request["source"]["owner"]
        or recommendations[0].get("evidence_sha256") != request["source"]["evidence_sha256"]
        or recommendations[0].get("observed_state_revision") != request["review_revision"]
    ):
        raise ValueError("remote recommendation missing or replayed")
    launches = [
        e
        for e in events
        if e.get("action") == "review_assignment_launch"
        and (
            e.get("claim_revision") == launch["revision"]
            or e.get("recommendation_id") == event.get("recommendation_id")
        )
    ]
    if recorded and (len(launches) != 1 or launches[0] != event):
        raise ValueError("one exact native remote launch required")
    if not recorded and launches:
        raise ValueError("remote recommendation already consumed")
    # Replicate the immutable sealed route bytes through destination status,
    # relocating only the local file reference, never its hash or contents.
    snapshot = status["route_snapshot"]
    raw = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode()
    reference = dict(event["route_identity"]["production_snapshot"])
    from .source_bundle import _sha

    if (
        _sha(raw) != reference["sha256"]
        or snapshot["capacity_revision"] != reference["capacity_revision"]
    ):
        raise ValueError("remote route snapshot differs")
    path = Path(home) / "evidence/production-routes" / (reference["capacity_revision"] + ".json")
    _once(path, raw)
    reference["path"] = str(path)
    return reference
