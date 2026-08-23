#!/usr/bin/env python3
"""Migrate legacy single-file ITIL records to the conflict-free event layout.

Implements the migration algorithm from
``docs/itil-conflict-free-persistence.md`` section 7.  Idempotent, backed-up,
lossless.  Run ONCE on a single node (``noroc2027``) after backing up, or (for
testing) against a copy with ``--root``.

Pipeline per record type (incidents / problems / changes):
  1. Backup the whole ``itil/`` tree (tar, zstd if available) - skipped in
     ``--dry-run``.
  2. Per legacy ``<id>-<slug>.json`` (or ``<id>.json``): move it to
     ``_legacy/<type>/``, write ``<id>/core.json`` (if absent) from the
     immutable subset, and explode ``timeline[]`` (plus terminal
     ``acknowledged_at``/``resolved_at``/``closed_at``/``status`` facts) into
     per-writer ``events/<agent>.migrated.jsonl`` files with DETERMINISTIC
     event ids, so a re-run regenerates byte-identical output.
  3. Fold any ``.sync-conflict-*`` sibling into the base record's events
     (union-dedup by (ts, agent, action, note)).
  4. Semantic-merge duplicate clusters (same affected_services +
     normalized_title): canonical id = min(detected_at) then min(id); fold each
     duplicate's events under the canonical id in a namespaced
     ``events/<agent>.from-<origid>.migrated.jsonl`` file, add a ``merged_from``
     note, and drop a ``redirect.json`` stub at the duplicate id.
  5. Re-key auto-detected records (``source == service_health``) to their
     deterministic ids using ``detected_at``'s day bucket, leaving a redirect.
  6. Write a local-only ``migration.state.json`` (schema version + done ids);
     re-runs skip done ids unless ``--force``.

The migrated ``.jsonl`` files are always regenerated (overwritten, never
appended) from the ``_legacy`` source, which is why replay is a byte no-op.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Allow running from a source checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from skcapstone.itil import (  # noqa: E402
    _CHANGE_TRANSITIONS,
    _INCIDENT_TRANSITIONS,
    _PROBLEM_TRANSITIONS,
    _auto_incident_id,
    _slugify,
)

logger = logging.getLogger("itil_migrate")

SCHEMA_VERSION = 1
_MIGRATED_NODE = "migrated"

_TYPES = {
    "incidents": {"transitions": _INCIDENT_TRANSITIONS, "initial": "detected"},
    "problems": {"transitions": _PROBLEM_TRANSITIONS, "initial": "identified"},
    "changes": {"transitions": _CHANGE_TRANSITIONS, "initial": "proposed"},
}

# Immutable birth-fact keys copied verbatim into core.json, per type.
_CORE_KEYS = {
    "incidents": [
        "id",
        "title",
        "source",
        "affected_services",
        "impact",
        "managed_by",
        "created_by",
        "detected_at",
        "related_problem_id",
    ],
    "problems": [
        "id",
        "title",
        "managed_by",
        "created_by",
        "created_at",
        "related_incident_ids",
        "workaround",
    ],
    "changes": [
        "id",
        "title",
        "change_type",
        "risk",
        "rollback_plan",
        "test_plan",
        "managed_by",
        "created_by",
        "implementer",
        "cab_required",
        "related_problem_id",
        "created_at",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _det_event_id(record_id: str, writer: str, seq: int, ts: str, kind: str) -> str:
    """Return a deterministic event id (so re-runs are byte-identical)."""
    key = f"{record_id}|{writer}|{seq}|{ts}|{kind}"
    return hashlib.blake2b(key.encode("utf-8"), digest_size=16).hexdigest()


def _bridge_path(transitions: dict[str, set[str]], cur: str, target: str) -> list[str]:
    """Shortest chain of intermediate statuses from *cur* to *target* (BFS).

    Returns the list of statuses to step through (excluding *cur*, including
    *target*), or an empty list if already there or unreachable.
    """
    if cur == target:
        return []
    from collections import deque

    queue: deque[list[str]] = deque([[cur]])
    seen = {cur}
    while queue:
        path = queue.popleft()
        for nxt in sorted(transitions.get(path[-1], set())):
            if nxt in seen:
                continue
            new_path = path + [nxt]
            if nxt == target:
                return new_path[1:]
            seen.add(nxt)
            queue.append(new_path)
    return []


def _map_timeline_entry(entry: dict) -> Optional[dict]:
    """Map a legacy timeline entry to an event kind + payload (or None)."""
    action = str(entry.get("action", "") or "")
    note = entry.get("note", "") or ""
    if action in ("created", "proposed"):
        return {"kind": "created", "note": note}
    if action.startswith("status:"):
        _, _, rest = action.partition(":")
        to = rest.split("->")[-1].strip() if "->" in rest else rest.strip()
        return {"kind": "status", "to": to, "note": note}
    if action.startswith("severity:"):
        _, _, rest = action.partition(":")
        to = rest.split("->")[-1].strip() if "->" in rest else rest.strip()
        return {"kind": "severity", "to": to, "note": note}
    if action == "auto-approved":
        return {"kind": "auto-approved", "note": note}
    if action == "note":
        return {"kind": "note", "note": note}
    # Fallback: preserve as a note so nothing is lost.
    return {"kind": "note", "note": (note or action)}


def _events_from_legacy(rtype: str, data: dict) -> dict[str, list[dict]]:
    """Build per-writer event lists from a legacy record dict.

    Returns a mapping ``{writer: [event, ...]}`` where each event carries a
    global ``_order`` (source index) later folded into ``seq``.  The record's
    final status is guaranteed reachable by bridging intermediate transitions.
    """
    record_id = data["id"]
    cfg = _TYPES[rtype]
    per_writer: dict[str, list[dict]] = {}
    order = 0
    folded_status = cfg["initial"]

    timeline = data.get("timeline") or []
    last_ts = data.get("detected_at") or data.get("created_at") or _now()
    for entry in timeline:
        mapped = _map_timeline_entry(entry)
        if mapped is None:
            continue
        writer = entry.get("agent", "unknown") or "unknown"
        ts = entry.get("ts") or last_ts
        last_ts = ts
        ev = {
            "ts": ts,
            "writer": writer,
            "node": _MIGRATED_NODE,
            "kind": mapped["kind"],
            "_order": order,
        }
        for k, v in mapped.items():
            if k != "kind":
                ev[k] = v
        order += 1
        if mapped["kind"] == "status" and mapped.get("to") in cfg["transitions"].get(
            folded_status, set()
        ):
            folded_status = mapped["to"]
        per_writer.setdefault(writer, []).append(ev)

    # Synthesize terminal timestamp facts the timeline may not carry.
    synth: list[dict] = []
    if rtype == "incidents":
        if data.get("acknowledged_at"):
            synth.append({"ts": data["acknowledged_at"], "kind": "ack", "note": ""})
        if data.get("resolved_at"):
            synth.append(
                {
                    "ts": data["resolved_at"],
                    "kind": "status",
                    "to": "resolved",
                    "note": "",
                    "resolution_summary": data.get("resolution_summary"),
                }
            )
        if data.get("closed_at"):
            synth.append({"ts": data["closed_at"], "kind": "status", "to": "closed", "note": ""})

    # Bridge to the recorded final status so the fold reproduces it exactly.
    target = data.get("status")
    if target and target != folded_status:
        for hop in _bridge_path(cfg["transitions"], folded_status, target):
            synth.append({"ts": last_ts, "kind": "status", "to": hop, "note": ""})
            folded_status = hop

    for ev in synth:
        # Only bridge-apply status events that are actually valid from current.
        if (
            ev["kind"] == "status"
            and ev.get("to") not in cfg["transitions"].get(folded_status, set())
            and ev.get("to") != folded_status
        ):
            # ack/terminal facts remain useful even if not a valid transition.
            pass
        writer = "migration"
        ev2 = {
            "ts": ev["ts"],
            "writer": writer,
            "node": _MIGRATED_NODE,
            "kind": ev["kind"],
            "_order": order,
        }
        for k, v in ev.items():
            if k not in ("ts", "kind") and v is not None:
                ev2[k] = v
        order += 1
        per_writer.setdefault(writer, []).append(ev2)

    return per_writer


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _core_from_legacy(rtype: str, data: dict) -> dict:
    """Extract the immutable core subset from a legacy record dict."""
    core: dict[str, Any] = {"type": rtype[:-1]}
    for key in _CORE_KEYS[rtype]:
        if key in data and data[key] is not None:
            core[key] = data[key]
    core["label"] = _slugify(data.get("title", ""))
    if rtype == "incidents":
        core["severity_at_creation"] = data.get("severity", "sev3")
        if data.get("source") == "service_health":
            svc = (data.get("affected_services") or [data.get("title", "")])[0]
            core["dedup_key"] = f"{svc}:unreachable"
    core.setdefault("tags", data.get("tags", []))
    return core


def _write_jsonl(path: Path, events: list[dict], record_id: str, dry_run: bool) -> None:
    """Write (overwrite) a migrated per-writer jsonl file deterministically."""
    lines = []
    for ev in sorted(events, key=lambda e: e.get("_order", 0)):
        seq = ev.pop("_order", 0)
        out = {
            "event_id": _det_event_id(record_id, ev["writer"], seq, ev["ts"], ev["kind"]),
            "ts": ev["ts"],
            "writer": ev["writer"],
            "node": ev["node"],
            "seq": seq,
            "kind": ev["kind"],
        }
        for k, v in ev.items():
            if k not in out:
                out[k] = v
        lines.append(json.dumps(out, sort_keys=True))
    payload = "\n".join(lines) + ("\n" if lines else "")
    if dry_run:
        logger.info("[dry-run] would write %s (%d events)", path, len(lines))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


# ---------------------------------------------------------------------------
# Migration steps
# ---------------------------------------------------------------------------


def _iter_legacy_files(type_dir: Path) -> list[Path]:
    """Return legacy flat ``*.json`` files (not inside a record directory)."""
    if not type_dir.is_dir():
        return []
    out: list[Path] = []
    for f in sorted(type_dir.glob("*.json")):
        if f.is_file():
            out.append(f)
    return out


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skipping unreadable %s: %s", path, exc)
        return None


def _backup(itil_dir: Path, dry_run: bool) -> Optional[Path]:
    """tar (zstd if available) the itil tree. Returns the archive path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if shutil.which("zstd"):
        archive = itil_dir.parent / f"itil.pre-refactor-{ts}.tar.zst"
        cmd = ["tar", "--zstd", "-cf", str(archive), "-C", str(itil_dir.parent), itil_dir.name]
    else:
        archive = itil_dir.parent / f"itil.pre-refactor-{ts}.tar.gz"
        cmd = ["tar", "-czf", str(archive), "-C", str(itil_dir.parent), itil_dir.name]
    if dry_run:
        logger.info("[dry-run] would back up via: %s", " ".join(cmd))
        return archive
    subprocess.run(cmd, check=True)
    logger.info("Backed up to %s", archive)
    return archive


def _explode_record(itil_dir: Path, rtype: str, legacy_path: Path, dry_run: bool) -> Optional[str]:
    """Explode one legacy file into core.json + migrated event logs.

    Returns the record id, or None on failure.
    """
    data = _load_json(legacy_path)
    if not data or "id" not in data:
        return None
    record_id = data["id"]
    type_dir = itil_dir / rtype
    rec_dir = type_dir / record_id

    # core.json (write-if-absent)
    core = _core_from_legacy(rtype, data)
    core_path = rec_dir / "core.json"
    if not core_path.exists():
        if dry_run:
            logger.info("[dry-run] would write %s", core_path)
        else:
            rec_dir.mkdir(parents=True, exist_ok=True)
            core_path.write_text(
                json.dumps(core, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    # events (always regenerated from source -> replay-idempotent)
    per_writer = _events_from_legacy(rtype, data)
    for writer, events in per_writer.items():
        out = rec_dir / "events" / f"{writer}.migrated.jsonl"
        _write_jsonl(out, events, record_id, dry_run)

    # Retire the legacy file to _legacy/<type>/
    legacy_dest = itil_dir / "_legacy" / rtype / legacy_path.name
    if legacy_path.exists() and legacy_path.parent == type_dir:
        if dry_run:
            logger.info("[dry-run] would move %s -> %s", legacy_path, legacy_dest)
        else:
            legacy_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_path), str(legacy_dest))
    return record_id


def _resolve_redirect(type_dir: Path, record_id: str) -> str:
    """Follow redirect.json stubs to the canonical id (for stable re-runs)."""
    seen: set[str] = set()
    cur = record_id
    while cur not in seen:
        seen.add(cur)
        rec_dir = type_dir / cur
        if (rec_dir / "core.json").exists():
            return cur
        redirect = rec_dir / "redirect.json"
        if not redirect.exists():
            return cur
        try:
            cur = json.loads(redirect.read_text(encoding="utf-8"))["canonical"]
        except Exception:  # noqa: BLE001
            return cur
    return cur


def _fold_sync_conflicts(itil_dir: Path, rtype: str, dry_run: bool) -> None:
    """Fold ``*.sync-conflict-*.json`` siblings into their base record events."""
    type_dir = itil_dir / rtype
    legacy_dir = itil_dir / "_legacy" / rtype
    for search_dir in (type_dir, legacy_dir):
        if not search_dir.is_dir():
            continue
        for conflict in sorted(search_dir.glob("*.sync-conflict-*")):
            data = _load_json(conflict)
            if not data or "id" not in data:
                continue
            # Resolve through any merge/re-key redirect so a second run writes
            # to the same canonical location (byte-stable replay).
            record_id = _resolve_redirect(type_dir, data["id"])
            per_writer = _events_from_legacy(rtype, data)
            for writer, events in per_writer.items():
                out = (
                    itil_dir
                    / rtype
                    / record_id
                    / "events"
                    / f"{writer}.sync-conflict.migrated.jsonl"
                )
                _write_jsonl(out, events, record_id, dry_run)
            dest = legacy_dir / conflict.name
            if conflict.parent != legacy_dir:
                if dry_run:
                    logger.info("[dry-run] would retire %s", conflict)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(conflict), str(dest))


def _semantic_merge(itil_dir: Path, dry_run: bool) -> dict[str, str]:
    """Merge duplicate incident clusters into a deterministic canonical id.

    Returns a mapping ``{duplicate_id: canonical_id}``.
    """
    type_dir = itil_dir / "incidents"
    if not type_dir.is_dir():
        return {}
    # Gather (id -> core) for all exploded records.
    cores: dict[str, dict] = {}
    for rec_dir in sorted(type_dir.iterdir()):
        core_path = rec_dir / "core.json"
        if rec_dir.is_dir() and core_path.exists():
            core = _load_json(core_path)
            if core:
                cores[rec_dir.name] = core

    clusters: dict[tuple, list[str]] = {}
    for rid, core in cores.items():
        key = (
            tuple(sorted(core.get("affected_services") or [])),
            _slugify(core.get("title", "")),
        )
        clusters.setdefault(key, []).append(rid)

    redirects: dict[str, str] = {}
    for key, ids in clusters.items():
        if len(ids) < 2:
            continue
        canonical = min(ids, key=lambda r: (cores[r].get("detected_at", ""), r))
        dupes = [r for r in ids if r != canonical]
        for dup in dupes:
            dup_dir = type_dir / dup
            # Fold the duplicate's event files under the canonical id, namespaced.
            events_dir = dup_dir / "events"
            if events_dir.is_dir():
                for ef in sorted(events_dir.glob("*.jsonl")):
                    lines = [
                        line
                        for line in ef.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    dest = type_dir / canonical / "events" / f"{ef.stem}.from-{dup}.jsonl"
                    if dry_run:
                        logger.info("[dry-run] would fold %s -> %s", ef, dest)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_text(
                            "\n".join(lines) + ("\n" if lines else ""),
                            encoding="utf-8",
                        )
            # merged_from note on the canonical record.
            note_events = [
                {
                    "ts": cores[dup].get("detected_at", _now()),
                    "writer": "migration",
                    "node": _MIGRATED_NODE,
                    "kind": "note",
                    "note": f"merged_from:{dup}",
                    "_order": 0,
                }
            ]
            _write_jsonl(
                type_dir / canonical / "events" / f"merged-{dup}.jsonl",
                note_events,
                canonical,
                dry_run,
            )
            # redirect stub at the duplicate id; clear its core so lookups follow.
            redirects[dup] = canonical
            if dry_run:
                logger.info("[dry-run] would redirect %s -> %s", dup, canonical)
            else:
                (dup_dir / "redirect.json").write_text(
                    json.dumps({"canonical": canonical}, indent=2) + "\n",
                    encoding="utf-8",
                )
                # Retire the duplicate's core so _fold_record follows the redirect.
                dup_core = dup_dir / "core.json"
                if dup_core.exists():
                    retired = itil_dir / "_legacy" / "merged" / f"{dup}.core.json"
                    retired.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dup_core), str(retired))
    return redirects


def _rekey_auto_detected(itil_dir: Path, dry_run: bool) -> dict[str, str]:
    """Re-key surviving auto-detected incidents to their deterministic ids."""
    type_dir = itil_dir / "incidents"
    if not type_dir.is_dir():
        return {}
    rekeys: dict[str, str] = {}
    for rec_dir in sorted(type_dir.iterdir()):
        core_path = rec_dir / "core.json"
        if not (rec_dir.is_dir() and core_path.exists()):
            continue
        core = _load_json(core_path)
        if not core or core.get("source") != "service_health":
            continue
        svc = (core.get("affected_services") or [core.get("title", "")])[0]
        dedup = core.get("dedup_key", f"{svc}:unreachable")
        failure_class = dedup.split(":")[-1] if ":" in dedup else "unreachable"
        detected = core.get("detected_at", _now())
        try:
            day = datetime.fromisoformat(detected.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new_id = _auto_incident_id(svc, failure_class, day)
        if new_id == rec_dir.name:
            continue
        new_dir = type_dir / new_id
        rekeys[rec_dir.name] = new_id
        if dry_run:
            logger.info("[dry-run] would re-key %s -> %s", rec_dir.name, new_id)
            continue
        if new_dir.exists():
            # Merge events in; leave redirect.
            for ef in (rec_dir / "events").glob("*.jsonl"):
                shutil.move(str(ef), str(new_dir / "events" / f"rekey-{ef.name}"))
        else:
            new_core = dict(core)
            new_core["id"] = new_id
            new_dir.mkdir(parents=True, exist_ok=True)
            (new_dir / "events").mkdir(exist_ok=True)
            (new_dir / "core.json").write_text(
                json.dumps(new_core, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            for ef in (rec_dir / "events").glob("*.jsonl"):
                shutil.move(str(ef), str(new_dir / "events" / ef.name))
            # Retire old core, drop redirect.
            retired = itil_dir / "_legacy" / "rekey" / f"{rec_dir.name}.core.json"
            retired.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(core_path), str(retired))
        (rec_dir / "redirect.json").write_text(
            json.dumps({"canonical": new_id}, indent=2) + "\n", encoding="utf-8"
        )
    return rekeys


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def migrate(root: Path, dry_run: bool = False, force: bool = False) -> dict:
    """Run the full migration against ``<root>/coordination/itil/``.

    Args:
        root: The shared root (contains ``coordination/itil/``).
        dry_run: Log intended actions without writing.
        force: Reprocess ids already recorded in ``migration.state.json``.

    Returns:
        A summary dict (counts + redirects).
    """
    itil_dir = Path(root).expanduser() / "coordination" / "itil"
    if not itil_dir.is_dir():
        logger.error("No ITIL directory at %s", itil_dir)
        return {"error": "no itil dir"}

    state_path = itil_dir / "migration.state.json"
    state: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "done": []}
    if state_path.exists():
        loaded = _load_json(state_path)
        if loaded:
            state = loaded
    done: set[str] = set() if force else set(state.get("done", []))

    _backup(itil_dir, dry_run)

    exploded: list[str] = []
    for rtype in _TYPES:
        for legacy in _iter_legacy_files(itil_dir / rtype):
            if "sync-conflict" in legacy.name:
                continue
            data = _load_json(legacy)
            if not data or "id" not in data:
                continue
            if data["id"] in done:
                logger.info("Skipping already-migrated %s", data["id"])
                continue
            rid = _explode_record(itil_dir, rtype, legacy, dry_run)
            if rid:
                exploded.append(rid)

    # Merge + re-key first so redirects exist, then fold sync-conflicts into the
    # resolved canonical id. This keeps the deterministic event ids (which hash
    # the record id) identical on every replay - a true byte no-op on re-run.
    redirects = _semantic_merge(itil_dir, dry_run)
    rekeys = _rekey_auto_detected(itil_dir, dry_run)

    for rtype in _TYPES:
        _fold_sync_conflicts(itil_dir, rtype, dry_run)

    if not dry_run:
        state["schema_version"] = SCHEMA_VERSION
        state["done"] = sorted(set(state.get("done", [])) | set(exploded))
        state["last_run"] = _now()
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary = {
        "exploded": len(exploded),
        "merged_redirects": len(redirects),
        "rekeys": len(rekeys),
        "dry_run": dry_run,
    }
    logger.info("Migration summary: %s", summary)
    return summary


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default="~/.skcapstone",
        help="Shared root containing coordination/itil/ (default: ~/.skcapstone)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions, write nothing")
    parser.add_argument(
        "--force", action="store_true", help="Reprocess ids already in migration.state.json"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    summary = migrate(Path(args.root), dry_run=args.dry_run, force=args.force)
    return 0 if "error" not in summary else 1


if __name__ == "__main__":
    raise SystemExit(main())
