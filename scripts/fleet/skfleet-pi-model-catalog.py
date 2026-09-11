#!/usr/bin/env python3
"""Bootstrap non-secret SKGateway models and logical routes in Pi's catalog."""

from __future__ import annotations

import argparse
import copy
import errno
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ALIASES = {
    "sk-glm-s": ("glm-4.6", "GLM small via SKGateway"),
    "sk-glm-m": ("glm-4.6", "GLM medium via SKGateway"),
    "sk-glm-l": ("glm-4.7", "GLM large via SKGateway"),
    "sk-zai-s": ("glm-4.6", "Z.ai small via SKGateway"),
    "sk-zai-m": ("glm-4.6", "Z.ai medium via SKGateway"),
    "sk-zai-l": ("glm-4.7", "Z.ai large via SKGateway"),
}

SOURCE_MODELS = (
    {
        "id": "glm-4.6",
        "name": "GLM-4.6 via SKGateway (z.ai)",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 200000,
    },
    {
        "id": "glm-4.7",
        "name": "GLM-4.7 via SKGateway (z.ai)",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 200000,
    },
    {
        "id": "glm-5.3",
        "name": "GLM-5.3 via SKGateway (z.ai)",
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 200000,
    },
    {
        "id": "kimi-for-coding",
        "name": "Kimi K2.7 Coding via SKGateway (z.ai-free kimi sub)",
        "reasoning": True,
        "input": ["text", "image"],
        "contextWindow": 262144,
    },
    {
        "id": "kimi-for-coding-highspeed",
        "name": "Kimi K2.7 Coding Highspeed via SKGateway",
        "reasoning": True,
        "input": ["text", "image"],
        "contextWindow": 262144,
    },
    {
        "id": "k3",
        "name": "Kimi K3 via SKGateway (1M context)",
        "reasoning": True,
        "input": ["text", "image"],
        "contextWindow": 1000000,
    },
    {
        "id": "k3-256k",
        "name": "Kimi K3 256K via SKGateway",
        "reasoning": True,
        "input": ["text", "image"],
        "contextWindow": 262144,
    },
)


def catalog_path() -> Path:
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    return root / "models.json"


def _secure_regular_file(path: Path) -> os.stat_result:
    """Validate and normalize the catalog without ever following a symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("catalog must not be a symlink") from exc
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("catalog must be a regular file")
        if info.st_uid != os.getuid():
            raise ValueError("catalog must be owned by the current user")
        # Pi's catalog is user-owned data.  Normalize a live 0664 file before
        # parsing or reconciling it, while retaining ownership and contents.
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
        return info
    finally:
        os.close(descriptor)


def reconcile(document: dict) -> tuple[dict, list[str]]:
    providers = document.get("providers")
    gateway = providers.get("skgateway") if isinstance(providers, dict) else None
    models = gateway.get("models") if isinstance(gateway, dict) else None
    if not isinstance(models, list) or not all(isinstance(item, dict) for item in models):
        raise ValueError("providers.skgateway.models must be a list of objects")
    for item in models:
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("every model id must be a non-empty string")
    managed_ids = {item["id"] for item in SOURCE_MODELS} | set(ALIASES)
    seen_managed = set()
    for item in models:
        model_id = item.get("id")
        if model_id not in managed_ids:
            continue
        if model_id in seen_managed:
            raise ValueError(f"duplicate managed model id: {model_id}")
        seen_managed.add(model_id)
    updated = copy.deepcopy(document)
    target_models = updated["providers"]["skgateway"]["models"]
    target_by_id = {item.get("id"): item for item in target_models}
    changed = []
    for expected in SOURCE_MODELS:
        model_id = expected["id"]
        current = target_by_id.get(model_id)
        if current is None:
            added = copy.deepcopy(expected)
            target_models.append(added)
            target_by_id[model_id] = added
            changed.append(model_id)
        elif current != expected:
            raise ValueError(f"conflicting source model metadata: {model_id}")

    for alias, (source, name) in ALIASES.items():
        expected = copy.deepcopy(target_by_id[source])
        expected["id"] = alias
        expected["name"] = name
        current = target_by_id.get(alias)
        if current == expected:
            continue
        if current is None:
            target_models.append(expected)
        else:
            raise ValueError(f"conflicting logical alias metadata: {alias}")
        changed.append(alias)
    return updated, changed


def load_and_reconcile(path: Path) -> tuple[dict, list[str], os.stat_result]:
    info = _secure_regular_file(path)
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("catalog root must be an object")
    updated, changed = reconcile(document)
    return updated, changed, info


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=catalog_path())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        updated, changed, info = load_and_reconcile(args.catalog)
        if changed and args.apply:
            write_atomic(args.catalog, updated, info)
    except (OSError, UnicodeError, ValueError) as exc:
        detail = " ".join(str(exc).split()) or type(exc).__name__
        print(f"PI_MODEL_CATALOG_ERROR|{type(exc).__name__}|{detail[:200]}", file=sys.stderr)
        return 2
    state = "changed" if changed else "current"
    print(
        f"PI_MODEL_CATALOG|{state}|sources={len(SOURCE_MODELS)}|"
        f"aliases={len(ALIASES)}|pending={len(changed)}"
    )
    return 1 if changed and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
