#!/usr/bin/env python3
"""Refresh the `skgateway` provider block in Pi's models.json from the live gateway.

Called by the `pi` wrapper in sk-agent-picker.sh so every Pi launch sees the
models SKGateway is actually serving right now — buckets and roles included —
instead of whatever list was hand-written into ~/.pi/agent/models.json.

Unlike skfleet-pi-model-catalog.py (which bootstraps a fixed set of logical
routes and refuses to touch existing metadata), this is a forced refresh: the
gateway is the source of truth and the provider's model list is replaced.
Every other provider in the catalog is left untouched.

Stdlib only, so it runs on the system interpreter without a venv.

Env knobs:
  SK_GATEWAY_URL             gateway base URL (default http://localhost:18780)
  SK_PI_GATEWAY_PROVIDER     provider key to rewrite (default skgateway)
  PI_CODING_AGENT_DIR        Pi agent dir (default ~/.pi/agent)
  SK_PI_SYNC_TIMEOUT         fetch timeout, seconds (default 5)
  SK_PI_SYNC_SKIP_STALE      1 = drop models the gateway flags stale
  SK_PI_SYNC_ONLY            regex; keep only model ids that match
  SK_PI_SYNC_DEFAULT_CTX     contextWindow for models with no card (default 131072)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_GATEWAY = "http://localhost:18780"
DEFAULT_MAX_TOKENS = 8192
BACKUP_KEEP = 10


def catalog_path() -> Path:
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    return root / "models.json"


def gateway_url() -> str:
    return os.environ.get("SK_GATEWAY_URL", DEFAULT_GATEWAY).rstrip("/")


def _secure_regular_file(path: Path) -> os.stat_result:
    """Same safety contract skfleet-pi-model-catalog.py enforces on the catalog."""
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


def fetch_models(base: str, timeout: float) -> list[dict]:
    request = urllib.request.Request(f"{base}/v1/models", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("gateway /v1/models did not return a data list")
    return [item for item in data if isinstance(item, dict) and item.get("id")]


def bucket_name(entry: dict) -> str:
    """Readable label for the gateway's own logical routes, which carry no card."""
    bits = [entry["id"], "— SKGateway"]
    kind = entry.get("kind")
    if kind == "bucket":
        label = entry.get("model_class") or "?"
        sensitivity = entry.get("sensitivity")
        bits.append(f"{label} bucket" + (f"/{sensitivity}" if sensitivity else ""))
    elif kind:
        bits.append(str(kind))
    return " ".join(bits)


def to_pi_model(entry: dict, previous: dict[str, dict], default_ctx: int) -> dict:
    """Map one gateway /v1/models entry onto Pi's model schema.

    The gateway wins on everything it actually reports. Where it is silent —
    the logical sk-* buckets carry no card at all — the value already in the
    catalog is kept, so hand-tuned local metadata survives the refresh instead
    of collapsing to a generic default.
    """
    model_id = entry["id"]
    card = entry.get("card") if isinstance(entry.get("card"), dict) else {}
    supported = card.get("supported_parameters") or []
    prior = previous.get(model_id) or {}

    vision = entry.get("vision")
    if vision is None:
        vision = card.get("vision")
    reasoning = card.get("reasoning")
    if reasoning is None and supported:
        reasoning = "reasoning" in supported

    model = {
        "id": model_id,
        "name": card.get("display_name")
        or (bucket_name(entry) if entry.get("kind") else prior.get("name") or model_id),
        "reasoning": bool(prior.get("reasoning", False) if reasoning is None else reasoning),
        "input": (
            ["text", "image"]
            if vision
            else (prior.get("input", ["text"]) if vision is None else ["text"])
        ),
        "contextWindow": int(
            entry.get("ctx_tokens")
            or card.get("context_length")
            or prior.get("contextWindow")
            or default_ctx
        ),
        "maxTokens": int(
            card.get("max_output_tokens") or prior.get("maxTokens") or DEFAULT_MAX_TOKENS
        ),
    }
    min_output = card.get("min_output_tokens") or prior.get("min_output_tokens")
    if min_output:
        model["min_output_tokens"] = int(min_output)

    # The gateway reports no per-token cost for Pi's accounting, so keep whatever
    # the catalog already had for this id rather than zeroing out a paid model.
    model["cost"] = copy.deepcopy(
        prior.get("cost", {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0})
    )
    return model


def select(entries: list[dict]) -> list[dict]:
    if os.environ.get("SK_PI_SYNC_SKIP_STALE") == "1":
        entries = [item for item in entries if not item.get("stale")]
    entries = [item for item in entries if item.get("advertised", True)]
    only = os.environ.get("SK_PI_SYNC_ONLY")
    if only:
        pattern = re.compile(only)
        entries = [item for item in entries if pattern.search(item["id"])]
    return entries


def reconcile(document: dict, entries: list[dict], provider: str, base: str) -> tuple[dict, bool]:
    providers = document.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("catalog providers must be an object")

    updated = copy.deepcopy(document)
    block = updated["providers"].get(provider)
    if not isinstance(block, dict):
        block = {}
    existing = block.get("models")
    previous = (
        {item["id"]: item for item in existing if isinstance(item, dict) and item.get("id")}
        if isinstance(existing, list)
        else {}
    )

    default_ctx = int(os.environ.get("SK_PI_SYNC_DEFAULT_CTX", "131072"))
    block.setdefault("baseUrl", f"{base}/v1")
    block.setdefault("api", "openai-completions")
    block.setdefault("apiKey", "not-needed")
    block["models"] = [to_pi_model(item, previous, default_ctx) for item in entries]
    updated["providers"][provider] = block

    return updated, updated != document


def write_atomic(path: Path, document: dict, info: os.stat_result) -> None:
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


def back_up(path: Path, info: os.stat_result) -> None:
    backups = path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    snapshot = backups / f"{path.name}.{int(os.path.getmtime(path))}.bak"
    snapshot.write_bytes(path.read_bytes())
    os.chmod(snapshot, stat.S_IMODE(info.st_mode))
    for stale in sorted(backups.glob(f"{path.name}.*.bak"))[:-BACKUP_KEEP]:
        stale.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=catalog_path())
    parser.add_argument("--gateway", default=gateway_url())
    parser.add_argument(
        "--provider", default=os.environ.get("SK_PI_GATEWAY_PROVIDER", "skgateway")
    )
    parser.add_argument(
        "--timeout", type=float, default=float(os.environ.get("SK_PI_SYNC_TIMEOUT", "5"))
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without writing (exit 1 when the catalog is stale)",
    )
    args = parser.parse_args()

    base = args.gateway.rstrip("/")
    try:
        entries = select(fetch_models(base, args.timeout))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        print(
            f"PI_GATEWAY_SYNC_UNAVAILABLE|{type(exc).__name__}|{detail[:200]}",
            file=sys.stderr,
        )
        return 0  # the gateway being down must never block a Pi launch

    if not entries:
        print(
            "PI_GATEWAY_SYNC_UNAVAILABLE|EmptyCatalog|gateway advertised no models",
            file=sys.stderr,
        )
        return 0

    try:
        info = _secure_regular_file(args.catalog)
        document = json.loads(args.catalog.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("catalog root must be an object")
        updated, changed = reconcile(document, entries, args.provider, base)
        if changed and not args.check:
            back_up(args.catalog, info)
            write_atomic(args.catalog, updated, info)
    except (OSError, UnicodeError, ValueError) as exc:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        print(f"PI_GATEWAY_SYNC_ERROR|{type(exc).__name__}|{detail[:200]}", file=sys.stderr)
        return 2

    state = "changed" if changed else "current"
    print(f"PI_GATEWAY_SYNC|{state}|provider={args.provider}|models={len(entries)}")
    return 1 if changed and args.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
