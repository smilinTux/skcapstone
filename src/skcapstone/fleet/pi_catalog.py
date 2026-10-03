"""Materialize Pi transport metadata from one fresh, sealed gateway observation."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from ..fleet_lane_health import MAX_AGE_SECONDS
from .production_dispatch import production_lanes
from .review_capacity import _review_capacity_truth_is_current

_BOUND = 4 * 1024 * 1024


def _read_private(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(
                "Pi catalog must be an owned regular file without write access for others"
            )
        raw = stream.read(_BOUND + 1)
    if len(raw) > _BOUND:
        raise ValueError("Pi catalog exceeds bound")
    return raw


def _model(route: dict) -> dict:
    """Adapt actual advertised fields; never invent model IDs or token limits."""
    row = route.get("gateway_model")
    if not isinstance(row, dict):
        raise ValueError("gateway observation lacks exact model metadata")
    model = row.get("id")
    card = row.get("card")
    if (
        not isinstance(model, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model)
        or model != route.get("model_or_bucket")
        or row.get("advertised") is not True
        or row.get("stale") is not False
        or row.get("tools") is not True
        or not isinstance(card, dict)
        or card.get("reasoning") is not True
        or (row.get("provider") or row.get("owned_by")) != route.get("provider")
    ):
        raise ValueError("gateway model metadata does not match qualified route")
    value = {
        "id": model,
        "name": card.get("display_name") or model,
        "reasoning": card["reasoning"],
        "input": ["text"] + (["image"] if row.get("vision") is True else []),
    }
    for source, target in (
        (card.get("context_length") or row.get("ctx_tokens"), "contextWindow"),
        (card.get("generation_default_tokens") or card.get("max_output_tokens"), "maxTokens"),
    ):
        if source is not None:
            if type(source) is not int or source <= 0:
                raise ValueError("gateway model limit is invalid")
            value[target] = source
    if route.get("provider") == "deepseek":
        value["compat"] = {"thinkingFormat": "deepseek"}
    return value


def materialize_gateway_catalog(home: Path, policy: dict, snapshot: dict) -> dict:
    """Update only Pi's gateway provider, with node-private rollback bytes.

    The dispatcher still chooses a model from the same observation against the
    exact card contract. This adapter grants no card or provider authorization.
    """
    observed = snapshot.get("observed_at")
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("error") is not None
        or not _review_capacity_truth_is_current(snapshot)
        or type(observed) not in (int, float)
        or not 0 <= time.time() - observed <= MAX_AGE_SECONDS
        or snapshot.get("endpoint", "").rstrip("/") != policy["gateway_url"].rstrip("/")
    ):
        raise ValueError("Pi catalog requires the current sealed gateway observation")
    domains = {
        domain
        for lane in production_lanes(policy, 1)
        if lane["target"]
        for domain in lane["capacity_domains"]
    }
    models = {}
    for route in snapshot.get("routes", []):
        if route.get("capacity_domain") not in domains or route.get("state") != "healthy":
            continue
        model = _model(route)
        if model["id"] in models and models[model["id"]] != model:
            raise ValueError("ambiguous gateway model metadata")
        models[model["id"]] = model
    if not models:
        raise ValueError("Pi catalog has no qualified enabled gateway models")
    path = Path(home) / ".pi/agent/models.json"
    lock = os.open(
        path.with_suffix(".json.lock"),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError("Pi catalog lock is not regular")
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = _read_private(path)
        value = json.loads(raw)
        providers = value.get("providers")
        if not isinstance(providers, dict):
            raise ValueError("Pi provider catalog is malformed")
        endpoint = policy["gateway_url"].rstrip("/") + "/v1"
        existing = providers.get("skgateway")
        candidates = (
            [existing]
            if existing is not None
            else [
                row
                for row in providers.values()
                if isinstance(row, dict) and row.get("baseUrl", "").rstrip("/") == endpoint
            ]
        )
        if (
            not candidates
            or any(
                not isinstance(row, dict)
                or row.get("baseUrl", "").rstrip("/") != endpoint
                or not isinstance(row.get("apiKey"), str)
                or not row["apiKey"]
                for row in candidates
            )
            or len({row["apiKey"] for row in candidates}) != 1
        ):
            raise ValueError("Pi requires one existing private gateway credential binding")
        gateway = copy.deepcopy(candidates[0])
        gateway.update(
            baseUrl=endpoint,
            api="openai-completions",
            models=[models[key] for key in sorted(models)],
        )
        replacement = copy.deepcopy(value)
        replacement["providers"]["skgateway"] = gateway
        result = {
            "path": str(path),
            "models": sorted(models),
            "snapshot_revision": snapshot["capacity_revision"],
            "changed": replacement != value,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if replacement == value:
            return result
        backup = path.with_name("models.json.before-" + str(time.time_ns()))
        with os.fdopen(
            os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb"
        ) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        encoded = (json.dumps(replacement, indent=2) + "\n").encode()
        fd, temporary = tempfile.mkstemp(prefix=".models-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if _read_private(path) != raw:
                raise ValueError("Pi catalog changed outside its update lock")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        result.update(sha256=hashlib.sha256(encoded).hexdigest(), backup_name=backup.name)
        return result
    finally:
        os.close(lock)
