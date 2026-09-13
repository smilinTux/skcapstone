#!/usr/bin/env python3
"""Reconcile Pi's SKGateway catalog to currently advertised gateway routes.

Ownership boundary:
  - SKGateway ``/v1/models`` (plus optional runtime revision) is authoritative.
  - ``~/.pi/agent/models.json`` is a local cache Pi reads at session start.
  - ``~/.pi/agent/settings.json`` ``defaultModel`` is Pi's selection default.

Stale served-model names left in the cache after a gateway rollout produce
HTTP 404 ``unknown_model`` before an eligible review can claim. This helper
selects only currently advertised routes, invalidates removed entries when the
gateway revision or inventory fingerprint changes, and falls default selection
back across policy-compatible size capacity without naming hosts or concrete
served models in product contracts.
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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

MAX_RESPONSE_BYTES = 65_536
DEFAULT_TIMEOUT_SECONDS = 12.0
SYNC_KEY = "skfleet_catalog_sync"
SIZE_FALLBACK = ("sk-s", "sk-m", "sk-l", "sk-xl")
_SIZE_RANK = {"S": 0, "M": 1, "L": 2, "XL": 3}


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


def fetch_model_inventory(
    base_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> list[dict[str, Any]]:
    """Return the gateway model rows used for Pi catalog reconciliation."""
    endpoint = base_url.rstrip("/")
    try:
        with opener(endpoint + "/v1/models", timeout=timeout) as response:
            document = _json_object(response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"gateway inventory rejected with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError(f"gateway inventory unavailable: {type(exc).__name__}") from None
    rows = document.get("data")
    if not isinstance(rows, list):
        raise ValueError("gateway inventory data must be a list")
    return [row for row in rows if isinstance(row, dict)]


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


def inventory_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    """Stable digest of currently advertised route identities."""
    ids = sorted({str(row["id"]).strip() for row in rows if is_currently_advertised(row)})
    payload = json.dumps(ids, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


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


def advertised_records(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Provider-neutral Pi records for currently advertised routes only."""
    records = []
    seen: set[str] = set()
    for row in rows:
        if not is_currently_advertised(row):
            continue
        record = _pi_model_record(row)
        route = record["id"]
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
    """Pick the cheapest policy-compatible logical size capacity still advertised."""
    available = {item.strip() for item in advertised_ids if isinstance(item, str)}
    start = _SIZE_RANK.get((required_size or "S").upper(), 0)
    for route in SIZE_FALLBACK[start:]:
        if route in available:
            return route
    for route in SIZE_FALLBACK[:start]:
        if route in available:
            return route
    ordered = sorted(available)
    return ordered[0] if ordered else None


def reconcile(
    document: dict[str, Any],
    inventory: Sequence[Mapping[str, Any]],
    *,
    gateway_revision: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Replace skgateway models with current advertised inventory; drop stale ids."""
    providers = document.get("providers")
    gateway = providers.get("skgateway") if isinstance(providers, dict) else None
    models = gateway.get("models") if isinstance(gateway, dict) else None
    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise ValueError("providers.skgateway.models must be a list of objects")
    for item in models:
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("every model id must be a non-empty string")

    records = advertised_records(inventory)
    if not records:
        raise ValueError("gateway inventory advertises no selectable routes")
    fingerprint = inventory_fingerprint(inventory)
    previous = gateway.get(SYNC_KEY) if isinstance(gateway.get(SYNC_KEY), dict) else {}
    previous_fingerprint = str(previous.get("inventory_fingerprint") or "")
    previous_revision = str(previous.get("gateway_revision") or "")
    inventory_changed = previous_fingerprint != fingerprint
    revision_changed = bool(gateway_revision) and previous_revision != gateway_revision

    updated = copy.deepcopy(document)
    target = updated["providers"]["skgateway"]
    old_ids = [str(item.get("id")) for item in target.get("models", [])]
    new_ids = [item["id"] for item in records]
    target["models"] = copy.deepcopy(records)
    target[SYNC_KEY] = {
        "schema_version": 1,
        "gateway_revision": gateway_revision,
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
    """Invalidate a stale defaultModel and fall back across size capacity."""
    available = {item.strip() for item in advertised_ids if isinstance(item, str) and item.strip()}
    current = settings.get("defaultModel")
    if isinstance(current, str) and current.strip() in available:
        return settings, None
    fallback = choose_fallback_route(sorted(available), required_size=required_size)
    if fallback is None:
        raise ValueError("no policy-compatible advertised route for defaultModel")
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
    inventory: Sequence[Mapping[str, Any]],
    *,
    gateway_revision: str = "",
) -> tuple[dict[str, Any], list[str], os.stat_result]:
    document, info = load_json_object(path)
    updated, changed = reconcile(document, inventory, gateway_revision=gateway_revision)
    return updated, changed, info


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
        if args.inventory is not None:
            payload = json.loads(args.inventory.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                rows = payload.get("data")
                if not isinstance(rows, list):
                    raise ValueError("inventory file data must be a list")
                inventory = [row for row in rows if isinstance(row, dict)]
            elif isinstance(payload, list):
                inventory = [row for row in payload if isinstance(row, dict)]
            else:
                raise ValueError("inventory file must be a list or models document")
        else:
            inventory = fetch_model_inventory(gateway_endpoint())
        updated, changed, info = load_and_reconcile(
            catalog,
            inventory,
            gateway_revision=str(args.gateway_revision or ""),
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
