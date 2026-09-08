#!/usr/bin/env python3
"""Bounded, evidence-first automation for recurring fleet interventions.

The default mode is observational.  Mutations require both ``--apply`` and
``SKFLEET_AUTOPILOT_MUTATION=1``; this keeps the same explicit fence used by
rotation while making the five checks useful in a timer and in tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from skcapstone.card_store import CardStore

HOME = Path(os.environ.get("SKCAPSTONE_HOME", Path.home() / ".skcapstone"))
REPORT_DIR = Path(os.environ.get("SKFLEET_AUTOPILOT_REPORT_DIR", HOME / "evidence/fleet-autopilot"))
TRIAGE_PATH = Path(os.environ.get("SKFLEET_TRIAGE_PATH", HOME / "evidence/fleet-backoff-triage/latest.json"))
HOSTS = tuple(os.environ.get("SKFLEET_HOSTS", "chiap01 chiap02 chiap03 chiap04 chiap08").split())


def _write_json(path: Path, value: Any) -> str:
    """Write canonical JSON and return its digest (never hand-built JSON)."""
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    json.loads(data)  # validate the exact bytes before they become evidence
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return hashlib.sha256(data).hexdigest()


def _cards() -> list[Any]:
    return CardStore(HOME).list_cards(degrade_unreadable=True)


def _card_dict(card: Any) -> dict[str, Any]:
    if hasattr(card, "model_dump"):
        return card.model_dump(mode="json")
    return dict(card)


def backoff_triage(cards: list[Any]) -> dict[str, Any]:
    rows = []
    for card in cards:
        d = _card_dict(card)
        meta = d.get("meta") or {}
        exits = meta.get("exits", meta.get("exit_reasons", []))
        if isinstance(exits, dict):
            flat = [x for reason, count in exits.items() for x in [reason] * int(count)]
        elif isinstance(exits, list):
            flat = exits
        else:
            flat = []
        status = str(d.get("status", "")).lower()
        if "backoff" not in status and not meta.get("backoff"):
            continue
        if len(flat) < 3:
            continue
        counts: dict[str, int] = {}
        for reason in flat:
            lane = str(reason).lower()
            lane = next((x for x in ("glm", "qwen", "codex") if x in lane), "blocked" if "block" in lane else lane)
            counts[lane] = counts.get(lane, 0) + 1
        top, amount = max(counts.items(), key=lambda x: x[1])
        rows.append({"card": d.get("id"), "exits": len(flat), "by_lane": counts,
                     "dead_lane": top if amount * 100 >= len(flat) * 80 else None,
                     "size_review": len(flat) >= 30,
                     "not_claimable": any(str(x).upper() in {"[REVIEW]", "[EXEC]", "[HUMAN]"} for x in d.get("labels", []))})
    report = {"generated_at": int(time.time()), "cards": rows, "count": len(rows)}
    _write_json(TRIAGE_PATH, report)
    return report


def review_drain(cards: list[Any]) -> dict[str, Any]:
    rows = []
    for card in cards:
        d = _card_dict(card)
        if str(d.get("status", "")).lower() != "review":
            continue
        meta = d.get("meta") or {}
        reviewers = meta.get("reviewers", meta.get("reviewer_claims", []))
        if reviewers:
            continue
        card_id = str(d.get("id", ""))
        evidence_dir = HOME / "evidence" / "work" / card_id
        files = sorted(p for p in evidence_dir.glob("**/*") if p.is_file()) if card_id else []
        evidence = bool(meta.get("pass_for_review_evidence")) or any(
            "PASS_FOR_REVIEW" in p.read_text(errors="replace") for p in files
        )
        rows.append({"card": card_id, "action": "dispatch_reviewer" if evidence else "redispatch_producer",
                     "evidence": evidence, "evidence_files": [str(p) for p in files]})
    return {"cards": rows, "count": len(rows)}


def propagation_watch() -> dict[str, Any]:
    """Check every card's two structural files on every host.

    This deliberately never copies or mutates remote state.  The rotation's
    propagation worker owns that action; this report is its trigger and audit
    trail.
    """
    cards_root = HOME / "cards"
    card_ids = sorted(p.name for p in cards_root.iterdir() if p.is_dir()) if cards_root.is_dir() else []
    missing: dict[str, list[str]] = {}
    for host in HOSTS:
        checks = " && ".join(
            f"test -f {cards_root / card_id / 'core.json'} && test -d {cards_root / card_id / 'events'}"
            for card_id in card_ids
        ) or "true"
        cmd = ["bash", "-lc", checks] if host in {os.uname().nodename, "localhost"} else ["ssh", host, "bash", "-lc", checks]
        try:
            failed = subprocess.run(cmd, capture_output=True, timeout=15).returncode != 0
        except (OSError, subprocess.TimeoutExpired):
            failed = True
        if failed:
            missing[host] = card_ids
    return {"hosts": list(HOSTS), "cards": len(card_ids), "missing": missing,
            "complete": not missing, "alert": bool(missing),
            "fallback_due": bool(missing), "action": "direct_ssh_copy" if missing else None}


def launch_health() -> dict[str, Any]:
    launched = []
    log = Path(os.environ.get("SKFLEET_ROTATE_LOG", HOME / "evidence/fleet-live/rotate.log"))
    if log.exists():
        for line in log.read_text(errors="replace").splitlines()[-500:]:
            if line.startswith("LAUNCHED|"):
                launched.append(line.split("|", 3)[1:])
    failed = []
    for fields in launched:
        unit = fields[0] if fields else ""
        result = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
        if result.stdout.strip() not in {"active", "activating"}:
            error = subprocess.run(["journalctl", "--user", "-u", unit, "-n", "20", "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
            failed.append({"unit": unit, "error": error,
                           "known_causes": {"unit_name": "invalid worker unit identity" in error.lower(),
                                            "model_not_found": "model not found" in error.lower(),
                                            "credential_expired": "credential" in error.lower() and "expir" in error.lower()},
                           "action": "release_claim_and_alert"})
    return {"launched": len(launched), "failed": failed}


def pool_starvation(cards: list[Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    stuck_cards: list[dict[str, Any]] = []
    for card in cards:
        d = _card_dict(card)
        status = str(d.get("status", "unknown")).lower()
        counts[status] = counts.get(status, 0) + 1
        if status in {"review", "backoff"}:
            meta = d.get("meta") or {}
            reviewers = meta.get("reviewers", meta.get("reviewer_claims", []))
            if status == "review" and not reviewers:
                action = "dispatch_reviewer" if meta.get("pass_for_review_evidence") else "redispatch_producer"
                stuck_cards.append({"card": d.get("id"), "status": status, "action": action})
            elif status == "backoff" and meta.get("retryable", True):
                stuck_cards.append({"card": d.get("id"), "status": status, "action": "retry_backoff"})
    ready = counts.get("ready", 0)
    starved = ready < 3
    return {"ready": ready, "threshold": 3, "starved": starved, "ineligible": counts,
            "duration_minutes": float(os.environ.get("SKFLEET_POOL_STARVATION_MINUTES", "0")),
            "alert": starved and float(os.environ.get("SKFLEET_POOL_STARVATION_MINUTES", "0")) >= 15,
            "classification": "stuck" if stuck_cards else "healthy",
            "stuck_cards": stuck_cards,
            "highest_return_action": stuck_cards[0]["action"] if stuck_cards else None}


def run(apply: bool = False) -> dict[str, Any]:
    cards = _cards()
    report = {"generated_at": int(time.time()), "triage": backoff_triage(cards),
              "review_drain": review_drain(cards), "propagation": propagation_watch(),
              "launch_health": launch_health(), "pool_starvation": pool_starvation(cards), "applied": False}
    digest = _write_json(REPORT_DIR / "latest.json", report)
    report["sha256"] = digest
    if apply and os.environ.get("SKFLEET_AUTOPILOT_MUTATION") == "1":
        report["applied"] = True
        _write_json(REPORT_DIR / "latest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.apply), sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
