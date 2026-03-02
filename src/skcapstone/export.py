"""Sovereign agent state export and import.

Produces a portable JSON bundle containing the full agent state:
identity, soul, memories, conversations, and config. Suitable for
migrating an agent to a new machine or sharing a snapshot.

Bundle schema (``bundle_version: 1``):

.. code-block:: json

    {
      "bundle_version": 1,
      "exported_at": "<ISO-8601>",
      "agent_name": "opus",
      "skcapstone_version": "0.9.0",
      "identity": { ... },
      "config": { ... },
      "soul": {
        "base": { ... },
        "active": { ... },
        "installed": { "soul-name": { ... } }
      },
      "memories": [ { "memory_id": ..., "content": ..., ... } ],
      "conversations": { "peer-name": [ { "role": ..., "content": ..., "timestamp": ... } ] }
    }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from . import __version__
from .memory_engine import list_memories, store as memory_store
from .models import MemoryLayer

logger = logging.getLogger("skcapstone.export")

BUNDLE_VERSION = 1

# Files relative to home that contain identity data
_IDENTITY_FILE = "identity/identity.json"
_CONFIG_FILE = "config/config.yaml"
_SOUL_BASE = "soul/base.json"
_SOUL_ACTIVE = "soul/active.json"
_SOUL_INSTALLED_DIR = "soul/installed"
_CONVERSATIONS_DIR = "conversations"


def export_bundle(home: Path) -> dict[str, Any]:
    """Export the full agent state as a portable JSON-serializable bundle.

    Collects identity, config, soul overlays, all memories, and all
    conversation histories from the agent home directory. Missing
    sections are included as empty dicts/lists rather than raising.

    Args:
        home: Agent home directory (e.g. ``~/.skcapstone``).

    Returns:
        dict: Fully serializable bundle, ready for ``json.dumps``.
    """
    home = Path(home).expanduser()

    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "agent_name": _read_agent_name(home),
        "skcapstone_version": __version__,
        "identity": _load_identity(home),
        "config": _load_config(home),
        "soul": _load_soul(home),
        "memories": _load_memories(home),
        "conversations": _load_conversations(home),
    }

    logger.info(
        "Exported bundle for %s: %d memories, %d conversations",
        bundle["agent_name"],
        len(bundle["memories"]),
        len(bundle["conversations"]),
    )
    return bundle


def import_bundle(
    home: Path,
    bundle: dict[str, Any],
    overwrite_identity: bool = False,
    overwrite_config: bool = False,
    overwrite_soul: bool = False,
) -> dict[str, Any]:
    """Import an agent state bundle into the target home directory.

    Memories are imported using duplicate-ID detection (existing memories
    are never overwritten). Conversations are merged per-peer, appending
    only messages not already present. Identity, config, and soul are
    written only when the corresponding flag is set or the file is absent.

    Args:
        home: Target agent home directory.
        bundle: Bundle dict as produced by :func:`export_bundle`.
        overwrite_identity: Overwrite ``identity/identity.json`` even if
            the file already exists.
        overwrite_config: Overwrite ``config/config.yaml`` even if it
            already exists.
        overwrite_soul: Overwrite soul files even if they already exist.

    Returns:
        dict: Import summary with keys ``memories_imported``,
        ``conversations_imported``, ``identity_written``,
        ``config_written``, ``soul_files_written``, ``errors``.
    """
    home = Path(home).expanduser()
    home.mkdir(parents=True, exist_ok=True)

    _validate_bundle(bundle)

    errors: list[str] = []
    summary: dict[str, Any] = {
        "memories_imported": 0,
        "conversations_imported": 0,
        "identity_written": False,
        "config_written": False,
        "soul_files_written": 0,
        "errors": errors,
    }

    # --- identity ---
    identity_path = home / _IDENTITY_FILE
    if bundle.get("identity") and (overwrite_identity or not identity_path.exists()):
        try:
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            identity_path.write_text(
                json.dumps(bundle["identity"], indent=2), encoding="utf-8"
            )
            summary["identity_written"] = True
        except OSError as exc:
            errors.append(f"identity write failed: {exc}")

    # --- config ---
    config_path = home / _CONFIG_FILE
    if bundle.get("config") and (overwrite_config or not config_path.exists()):
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                yaml.safe_dump(bundle["config"], default_flow_style=False),
                encoding="utf-8",
            )
            summary["config_written"] = True
        except OSError as exc:
            errors.append(f"config write failed: {exc}")

    # --- soul ---
    soul_section = bundle.get("soul") or {}
    soul_written = 0

    base_path = home / _SOUL_BASE
    if soul_section.get("base") and (overwrite_soul or not base_path.exists()):
        try:
            base_path.parent.mkdir(parents=True, exist_ok=True)
            base_path.write_text(
                json.dumps(soul_section["base"], indent=2), encoding="utf-8"
            )
            soul_written += 1
        except OSError as exc:
            errors.append(f"soul/base write failed: {exc}")

    active_path = home / _SOUL_ACTIVE
    if soul_section.get("active") and (overwrite_soul or not active_path.exists()):
        try:
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text(
                json.dumps(soul_section["active"], indent=2), encoding="utf-8"
            )
            soul_written += 1
        except OSError as exc:
            errors.append(f"soul/active write failed: {exc}")

    installed_dir = home / _SOUL_INSTALLED_DIR
    for soul_name, soul_data in (soul_section.get("installed") or {}).items():
        soul_file = installed_dir / f"{soul_name}.json"
        if overwrite_soul or not soul_file.exists():
            try:
                soul_file.parent.mkdir(parents=True, exist_ok=True)
                soul_file.write_text(json.dumps(soul_data, indent=2), encoding="utf-8")
                soul_written += 1
            except OSError as exc:
                errors.append(f"soul/installed/{soul_name} write failed: {exc}")

    summary["soul_files_written"] = soul_written

    # --- memories ---
    imported_memories = _import_memories(home, bundle.get("memories") or [])
    summary["memories_imported"] = imported_memories
    if imported_memories:
        errors_from_mem: list[str] = []  # import_memories logs but doesn't surface
        logger.info("Imported %d memories", imported_memories)

    # --- conversations ---
    imported_convs = _import_conversations(
        home, bundle.get("conversations") or {}, overwrite=overwrite_soul
    )
    summary["conversations_imported"] = imported_convs

    logger.info(
        "Import complete: %d memories, %d conversations, %d soul files",
        summary["memories_imported"],
        summary["conversations_imported"],
        summary["soul_files_written"],
    )
    return summary


# ---------------------------------------------------------------------------
# Private helpers — export
# ---------------------------------------------------------------------------


def _read_agent_name(home: Path) -> str:
    """Read the agent name from identity.json or config.yaml."""
    for rel in (_IDENTITY_FILE, _CONFIG_FILE):
        p = home / rel
        if p.exists():
            try:
                if p.suffix in (".yaml", ".yml"):
                    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                else:
                    data = json.loads(p.read_text(encoding="utf-8"))
                name = data.get("name") or data.get("agent_name")
                if name:
                    return str(name)
            except Exception:
                continue
    return "unknown"


def _load_identity(home: Path) -> dict:
    p = home / _IDENTITY_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Cannot read identity: %s", exc)
        return {}


def _load_config(home: Path) -> dict:
    p = home / _CONFIG_FILE
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Cannot read config: %s", exc)
        return {}


def _load_soul(home: Path) -> dict:
    soul: dict[str, Any] = {"base": {}, "active": None, "installed": {}}

    base_p = home / _SOUL_BASE
    if base_p.exists():
        try:
            soul["base"] = json.loads(base_p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Cannot read soul/base: %s", exc)

    active_p = home / _SOUL_ACTIVE
    if active_p.exists():
        try:
            soul["active"] = json.loads(active_p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Cannot read soul/active: %s", exc)

    installed_dir = home / _SOUL_INSTALLED_DIR
    if installed_dir.is_dir():
        for f in sorted(installed_dir.glob("*.json")):
            try:
                soul["installed"][f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Cannot read soul/installed/%s: %s", f.name, exc)

    return soul


def _load_memories(home: Path) -> list[dict]:
    """Load all memories from all layers."""
    entries = list_memories(home, limit=10000)
    result = []
    for e in entries:
        d = e.model_dump(mode="json")
        # Ensure datetimes are ISO strings
        for key in ("created_at", "accessed_at"):
            val = d.get(key)
            if val is not None and hasattr(val, "isoformat"):
                d[key] = val.isoformat()
        result.append(d)
    return result


def _load_conversations(home: Path) -> dict[str, list[dict]]:
    """Load all conversation histories from conversations/ dir."""
    conv_dir = home / _CONVERSATIONS_DIR
    if not conv_dir.is_dir():
        return {}

    conversations: dict[str, list[dict]] = {}
    for f in sorted(conv_dir.glob("*.json")):
        peer = f.stem
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                conversations[peer] = data
            elif isinstance(data, dict) and "messages" in data:
                conversations[peer] = data["messages"]
            else:
                conversations[peer] = []
        except Exception as exc:
            logger.warning("Cannot read conversation %s: %s", f.name, exc)

    return conversations


# ---------------------------------------------------------------------------
# Private helpers — import
# ---------------------------------------------------------------------------


def _validate_bundle(bundle: dict[str, Any]) -> None:
    """Raise ValueError if the bundle is structurally invalid."""
    if not isinstance(bundle, dict):
        raise ValueError("Bundle must be a JSON object")
    version = bundle.get("bundle_version")
    if version is None:
        raise ValueError("Missing bundle_version field")
    if version != BUNDLE_VERSION:
        raise ValueError(
            f"Unsupported bundle_version {version!r} (expected {BUNDLE_VERSION})"
        )


def _import_memories(home: Path, memory_list: list[dict]) -> int:
    """Import memories from a bundle, preserving original IDs for idempotency.

    Writes memory JSON files directly (bypassing store()) so the original
    memory_id is preserved. This makes re-importing the same bundle a no-op.
    """
    if not memory_list:
        return 0

    from .models import MemoryEntry
    from .memory_engine import _memory_dir, _update_index

    mem_dir = _memory_dir(home)  # creates layer subdirs

    # Build set of existing memory IDs from disk
    existing: set[str] = set()
    for layer in MemoryLayer:
        layer_dir = mem_dir / layer.value
        if layer_dir.is_dir():
            for f in layer_dir.glob("*.json"):
                existing.add(f.stem)

    imported = 0
    for mem_data in memory_list:
        mid = mem_data.get("memory_id", "")
        if not mid or mid in existing:
            continue
        try:
            layer_raw = mem_data.get("layer", "short-term")
            layer = MemoryLayer(layer_raw) if isinstance(layer_raw, str) else MemoryLayer.SHORT_TERM
            entry = MemoryEntry(
                memory_id=mid,
                content=mem_data["content"],
                tags=mem_data.get("tags") or [],
                source=mem_data.get("source", "bundle-import"),
                importance=float(mem_data.get("importance", 0.5)),
                layer=layer,
                metadata=mem_data.get("metadata") or {},
                soul_context=mem_data.get("soul_context"),
            )
            # Write with the original memory_id so re-import is idempotent
            path = mem_dir / layer.value / f"{mid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")
            _update_index(home, entry)
            existing.add(mid)
            imported += 1
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping invalid memory in bundle: %s", exc)

    return imported


def _import_conversations(
    home: Path, conversations: dict[str, list[dict]], overwrite: bool = False
) -> int:
    """Import conversation histories, merging per peer."""
    if not conversations:
        return 0

    conv_dir = home / _CONVERSATIONS_DIR
    conv_dir.mkdir(parents=True, exist_ok=True)

    imported = 0
    for peer, messages in conversations.items():
        if not peer or not isinstance(messages, list):
            continue

        peer_file = conv_dir / f"{peer}.json"
        existing_messages: list[dict] = []

        if peer_file.exists() and not overwrite:
            try:
                existing_data = json.loads(peer_file.read_text(encoding="utf-8"))
                if isinstance(existing_data, list):
                    existing_messages = existing_data
            except Exception:
                pass

        # Deduplicate by (role, content, timestamp) tuple
        existing_keys = {
            (m.get("role"), m.get("content"), m.get("timestamp"))
            for m in existing_messages
        }
        new_messages = [
            m for m in messages
            if (m.get("role"), m.get("content"), m.get("timestamp")) not in existing_keys
        ]

        merged = existing_messages + new_messages
        if new_messages or overwrite:
            try:
                peer_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
                imported += len(new_messages)
            except OSError as exc:
                logger.warning("Cannot write conversation %s: %s", peer, exc)

    return imported
