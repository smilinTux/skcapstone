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
