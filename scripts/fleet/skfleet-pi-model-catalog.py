#!/usr/bin/env python3
"""Reconcile Pi's SKGateway catalog to healthy advertised logical routes.

Ownership boundary:
  - SKGateway ``/v1/models``, ``/health``, ``/queue``, and runtime revision are
    authoritative.
  - ``~/.pi/agent/models.json`` is a local cache Pi reads at session start.
  - ``~/.pi/agent/settings.json`` ``defaultModel`` is Pi's selection default.

Only provider-neutral logical size routes (``sk-s``..``sk-xl``) that are
currently advertised and healthy are selectable. Concrete served-model IDs are
never installed into the cache. Required size never downgrades. The inventory
fingerprint covers route metadata and health, not IDs alone. The launcher must
supply ``SKFLEET_GATEWAY_URL`` and the active gateway revision; no host default
exists in this contract.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

MAX_RESPONSE_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 12.0
SYNC_KEY = "skfleet_catalog_sync"
SIZE_FALLBACK = ("sk-s", "sk-m", "sk-l", "sk-xl")
LOGICAL_ROUTES = frozenset(SIZE_FALLBACK)
_SIZE_RANK = {"S": 0, "M": 1, "L": 2, "XL": 3}
_ROUTE_SIZE = {"sk-s": "S", "sk-m": "M", "sk-l": "L", "sk-xl": "XL"}


def catalog_path() -> Path:
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    return root / "models.json"


def settings_path() -> Path:
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    return root / "settings.json"


def gateway_endpoint() -> str:
    raw = (os.environ.get("SKFLEET_GATEWAY_URL") or "").strip()
    if not raw:
        raise ValueError("SKFLEET_GATEWAY_URL is required")
    return raw.rstrip("/")


def _secure_regular_file(path: Path) -> os.stat_result:
    if path.is_symlink():
        raise ValueError("catalog must not be a symlink")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("catalog must be a regular file")
    if info.st_uid != os.getuid():
        raise ValueError("catalog must be owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("catalog must not be accessible by group or other")
    return info


def _json_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("gateway response exceeds bound")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("gateway response is not an object")
    return value


def _fetch_json(
    url: str,
    *,
    timeout: float,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    try:
        with opener(url, timeout=timeout) as response:
            return _json_object(response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"gateway rejected {url.rsplit('/', 1)[-1]} with HTTP {exc.code}"
        ) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"gateway {url.rsplit('/', 1)[-1]} unavailable: {type(exc).__name__}"
        ) from None


@dataclass(frozen=True)
class GatewayView:
    models: tuple[dict[str, Any], ...]
    health: Mapping[str, Any]
    queue: Mapping[str, Any]
    health_status: str
    observed_at: float


def fetch_gateway_view(
    base_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
    now: Callable[[], float] = time.time,
) -> GatewayView:
    """Fetch models, health, and queue for healthy logical-route selection."""
    endpoint = base_url.rstrip("/")
    models_doc = _fetch_json(endpoint + "/v1/models", timeout=timeout, opener=opener)
    health_doc = _fetch_json(endpoint + "/health", timeout=timeout, opener=opener)
    queue_doc = _fetch_json(endpoint + "/queue", timeout=timeout, opener=opener)
    rows = models_doc.get("data")
    health = health_doc.get("backends")
    queue = queue_doc.get("backends")
    if not isinstance(rows, list):
        raise ValueError("gateway inventory data must be a list")
    if health_doc.get("status") != "ok" or not isinstance(health, dict):
        raise ValueError("gateway health schema")
    if not isinstance(queue, dict):
        raise ValueError("gateway queue schema")
    return GatewayView(
        models=tuple(row for row in rows if isinstance(row, dict)),
        health=health,
        queue=queue,
        health_status=str(health_doc.get("status") or ""),
        observed_at=now(),
    )


def is_currently_advertised(row: Mapping[str, Any]) -> bool:
    """Match gateway preflight: missing advertised means present; stale fails."""
    route = row.get("id")
    if not isinstance(route, str) or not route.strip():
        return False
    if row.get("advertised") is False:
        return False
    if row.get("stale") is True:
        return False
    return True


def is_logical_route(route_id: str) -> bool:
    return route_id.strip() in LOGICAL_ROUTES


def _capacity_domain(queue: Mapping[str, Any], provider: str) -> str | None:
    matches = []
    for name, row in queue.items():
        if not isinstance(row, dict):
            continue
        members = row.get("members")
        if name == provider or (isinstance(members, list) and provider in members):
            if row.get("capacityDomain") == name:
                matches.append(name)
    return matches[0] if len(matches) == 1 else None


def _domain_healthy(
    health: Mapping[str, Any],
    queue: Mapping[str, Any],
    domain: str,
    observed_at: float,
) -> bool:
    health_row = health.get(domain) if isinstance(health, dict) else None
    queue_row = queue.get(domain) if isinstance(queue, dict) else None
    if not isinstance(health_row, dict) or not isinstance(queue_row, dict):
        return False
    if queue_row.get("capacityDomain") != domain or type(queue_row.get("max")) is not int:
        return False
    if health_row.get("quarantined") is not False:
        return False
    last_check = health_row.get("lastCheck")
    if (
        not isinstance(last_check, (int, float))
        or isinstance(last_check, bool)
        or last_check <= 0
        or last_check / 1000 - observed_at > 120
    ):
        return False
    if health_row.get("observed") is not True or health_row.get("status") not in {
        "up",
        "degraded",
    }:
        return False
    if queue_row["max"] <= 0:
        return False
    capacity = health_row.get("capacity")
    if (
        isinstance(capacity, dict)
        and capacity.get("current") is True
        and capacity.get("state") in {"throttled", "unavailable"}
    ):
        return False
    return True


def route_health_state(
    row: Mapping[str, Any],
    view: GatewayView,
) -> str:
    """Return healthy/unhealthy/unknown for one advertised logical route."""
    route = str(row.get("id") or "").strip()
    if not is_logical_route(route) or not is_currently_advertised(row):
        return "unhealthy"
    provider = str(row.get("provider") or row.get("owned_by") or "").strip()
    domain = _capacity_domain(view.queue, provider) if provider else None
    if domain is not None:
        return (
            "healthy"
            if _domain_healthy(view.health, view.queue, domain, view.observed_at)
            else "unhealthy"
        )
    # Logical buckets without a pinned domain still require live healthy capacity.
    if any(
        _domain_healthy(view.health, view.queue, str(name), view.observed_at)
        for name in view.queue
    ):
        return "healthy"
    return "unhealthy"


def _pi_model_record(row: Mapping[str, Any]) -> dict[str, Any]:
    route = str(row["id"]).strip()
    card = row.get("card") if isinstance(row.get("card"), dict) else {}
    name = row.get("name")
    if not isinstance(name, str) or not name.strip():
        name = f"{route} via SKGateway"
    record: dict[str, Any] = {
        "id": route,
        "name": name.strip(),
        "reasoning": card.get("reasoning") is True or row.get("tools") is True,
        "input": ["text"],
    }
    context = row.get("context_window") or row.get("contextWindow") or card.get("context_window")
    if isinstance(context, int) and not isinstance(context, bool) and context > 0:
        record["contextWindow"] = context
    return record


def _route_metadata(row: Mapping[str, Any], health_state: str) -> dict[str, Any]:
    """Fingerprint fields including Pi-emitted name/contextWindow."""
    card = row.get("card") if isinstance(row.get("card"), dict) else {}
    pi = _pi_model_record(row)
    return {
        "id": pi["id"],
        "name": pi["name"],
        "contextWindow": pi.get("contextWindow"),
        "advertised": False if row.get("advertised") is False else True,
        "stale": True if row.get("stale") is True else False,
        "provider": str(row.get("provider") or row.get("owned_by") or ""),
        "size_class": str(card.get("size_class") or _ROUTE_SIZE.get(pi["id"]) or ""),
        "tools": True if row.get("tools") is True else False,
        "reasoning": True if card.get("reasoning") is True else False,
        "health": health_state,
    }


def inventory_fingerprint(view: GatewayView) -> str:
    """Digest of logical-route metadata, Pi fields, and health — not IDs alone."""
    rows = []
    for row in view.models:
        route = str(row.get("id") or "").strip()
        if not is_logical_route(route):
            continue
        rows.append(_route_metadata(row, route_health_state(row, view)))
    payload = json.dumps(
        sorted(rows, key=lambda item: item["id"]), separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def healthy_logical_records(view: GatewayView) -> list[dict[str, Any]]:
    """Pi records for currently advertised healthy logical routes only."""
    records = []
    seen: set[str] = set()
    for row in view.models:
        route = str(row.get("id") or "").strip()
        if not is_logical_route(route):
            continue
        if route_health_state(row, view) != "healthy":
            continue
        record = _pi_model_record(row)
        if route in seen:
            raise ValueError(f"duplicate advertised route id: {route}")
        seen.add(route)
        records.append(record)
    return sorted(records, key=lambda item: item["id"])


def choose_fallback_route(
    advertised_ids: Sequence[str],
    *,
    required_size: str | None = None,
) -> str | None:
    """Pick the cheapest logical route at or above required size. Never downgrade."""
    available = {
        item.strip()
        for item in advertised_ids
        if isinstance(item, str) and is_logical_route(item.strip())
    }
    start = _SIZE_RANK.get((required_size or "S").upper(), 0)
    for route in SIZE_FALLBACK[start:]:
        if route in available:
            return route
    return None


def reconcile(
    document: dict[str, Any],
    view: GatewayView,
    *,
    gateway_revision: str,
) -> tuple[dict[str, Any], list[str]]:
    """Replace skgateway models with healthy logical routes; drop everything else."""
    if not isinstance(gateway_revision, str) or not gateway_revision.strip():
        raise ValueError("gateway revision is required")
    providers = document.get("providers")
    gateway = providers.get("skgateway") if isinstance(providers, dict) else None
    models = gateway.get("models") if isinstance(gateway, dict) else None
    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise ValueError("providers.skgateway.models must be a list of objects")
    for item in models:
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("every model id must be a non-empty string")

    records = healthy_logical_records(view)
    if not records:
        raise ValueError("gateway inventory advertises no healthy logical routes")
    fingerprint = inventory_fingerprint(view)
    previous = gateway.get(SYNC_KEY) if isinstance(gateway.get(SYNC_KEY), dict) else {}
    previous_fingerprint = str(previous.get("inventory_fingerprint") or "")
    previous_revision = str(previous.get("gateway_revision") or "")
    inventory_changed = previous_fingerprint != fingerprint
    revision_changed = previous_revision != gateway_revision.strip()

    updated = copy.deepcopy(document)
    target = updated["providers"]["skgateway"]
    old_ids = [str(item.get("id")) for item in target.get("models", [])]
    new_ids = [item["id"] for item in records]
    target["models"] = copy.deepcopy(records)
    target[SYNC_KEY] = {
        "schema_version": 2,
        "gateway_revision": gateway_revision.strip(),
        "inventory_fingerprint": fingerprint,
        "synced_ids": new_ids,
        "invalidated": bool(inventory_changed or revision_changed or old_ids != new_ids),
    }
    changed = []
    if old_ids != new_ids or inventory_changed or revision_changed:
        changed = sorted(set(old_ids).symmetric_difference(new_ids)) or ["inventory-revision"]
    return updated, changed


def repair_default_model(
    settings: dict[str, Any],
    advertised_ids: Sequence[str],
    *,
    required_size: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Invalidate a non-logical or undersized defaultModel; never downgrade."""
    available = {
        item.strip()
        for item in advertised_ids
        if isinstance(item, str) and is_logical_route(item.strip())
    }
    current = settings.get("defaultModel")
    required = (required_size or "S").upper()
    if (
        isinstance(current, str)
        and is_logical_route(current.strip())
        and current.strip() in available
    ):
        current_size = _ROUTE_SIZE[current.strip()]
        if _SIZE_RANK[current_size] >= _SIZE_RANK.get(required, 0):
            return settings, None
    fallback = choose_fallback_route(sorted(available), required_size=required)
    if fallback is None:
        raise ValueError("no policy-compatible healthy logical route for defaultModel")
    updated = copy.deepcopy(settings)
    updated["defaultModel"] = fallback
    if (
        not isinstance(updated.get("defaultProvider"), str)
        or not updated["defaultProvider"].strip()
    ):
        updated["defaultProvider"] = "skgateway"
    return updated, fallback


def load_json_object(path: Path) -> tuple[dict[str, Any], os.stat_result]:
    info = _secure_regular_file(path)
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} root must be an object")
    return document, info


def write_atomic(path: Path, document: dict[str, Any], info: os.stat_result) -> None:
    payload = (json.dumps(document, indent=2, ensure_ascii=True) + "\n").encode()
    json.loads(payload)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load_and_reconcile(
    path: Path,
    view: GatewayView,
    *,
    gateway_revision: str,
) -> tuple[dict[str, Any], list[str], os.stat_result]:
    document, info = load_json_object(path)
    updated, changed = reconcile(document, view, gateway_revision=gateway_revision)
    return updated, changed, info


def _view_from_inventory_file(path: Path, *, observed_at: float | None = None) -> GatewayView:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("inventory file must be an object with data/health/queue")
    rows = payload.get("data")
    health = payload.get("health") or payload.get("backends_health")
    queue = payload.get("queue") or payload.get("backends_queue")
    if isinstance(health, dict) and "backends" in health:
        health_status = str(health.get("status") or "ok")
        health = health.get("backends")
    else:
        health_status = str(payload.get("health_status") or "ok")
    if isinstance(queue, dict) and "backends" in queue:
        queue = queue.get("backends")
    if not isinstance(rows, list):
        raise ValueError("inventory file data must be a list")
    if not isinstance(health, dict) or not isinstance(queue, dict):
        raise ValueError("inventory file must include health and queue backends")
    return GatewayView(
        models=tuple(row for row in rows if isinstance(row, dict)),
        health=health,
        queue=queue,
        health_status=health_status,
        observed_at=time.time() if observed_at is None else observed_at,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=None)
    parser.add_argument("--settings", type=Path, default=None)
    parser.add_argument("--inventory", type=Path, default=None)
    parser.add_argument("--gateway-revision", default="")
    parser.add_argument("--required-size", default="S")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    catalog = args.catalog or catalog_path()
    settings = args.settings or settings_path()
    try:
        revision = str(args.gateway_revision or "").strip()
        if not revision:
            raise ValueError("gateway revision is required")
        if args.inventory is not None:
            view = _view_from_inventory_file(args.inventory)
        else:
            view = fetch_gateway_view(gateway_endpoint())
        updated, changed, info = load_and_reconcile(
            catalog,
            view,
            gateway_revision=revision,
        )
        advertised_ids = [item["id"] for item in updated["providers"]["skgateway"]["models"]]
        settings_changed = None
        settings_document = None
        settings_info = None
        if settings.exists():
            settings_document, settings_info = load_json_object(settings)
            settings_document, settings_changed = repair_default_model(
                settings_document,
                advertised_ids,
                required_size=str(args.required_size or "S"),
            )
        if args.apply:
            if changed:
                write_atomic(catalog, updated, info)
            if settings_changed and settings_document is not None and settings_info is not None:
                write_atomic(settings, settings_document, settings_info)
    except (OSError, UnicodeError, ValueError) as exc:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        print(f"PI_MODEL_CATALOG_ERROR|{type(exc).__name__}|{detail[:200]}", file=sys.stderr)
        return 2
    state = "changed" if changed or settings_changed else "current"
    print(
        "PI_MODEL_CATALOG|%s|advertised=%s|pending=%s|default=%s"
        % (
            state,
            len(advertised_ids),
            len(changed) + (1 if settings_changed else 0),
            settings_changed or "unchanged",
        )
    )
    return 1 if (changed or settings_changed) and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
