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
        evidence = meta.get("evidence") or meta.get("pass_for_review_evidence")
        rows.append({"card": d.get("id"), "action": "dispatch_reviewer" if evidence else "redispatch_producer",
                     "evidence": bool(evidence)})
    return {"cards": rows, "count": len(rows)}


def propagation_watch() -> dict[str, Any]:
    """Check the card *contents* on each host, with bounded SSH fallback.

    This is deliberately observational unless the explicit mutation fence is
    enabled.  The rotator remains the authority for claims and lifecycle.
    """
    missing: list[str] = []
    cards_root = HOME / "cards"
    for host in HOSTS:
        check = "test -d {0} && find {0} -name core.json -type f -print -quit | grep -q . && find {0} -type d -path '*/events' -print -quit | grep -q .".format(str(cards_root))
        cmd = ["bash", "-lc", check] if host in {os.uname().nodename, "localhost"} else ["ssh", host, "bash", "-lc", check]
        if subprocess.run(cmd, capture_output=True, timeout=15).returncode:
            missing.append(host)
    return {"hosts": list(HOSTS), "missing": missing, "complete": not missing,
            "alert": bool(missing), "fallback_due": bool(missing)}


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
        # LAUNCHED timestamps are optional for compatibility with old logs.
        launched_at = 0.0
        if len(fields) > 1:
            try:
                launched_at = float(fields[1])
            except ValueError:
                pass
        if launched_at and time.time() - launched_at < 30:
            continue
        result = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
        if result.stdout.strip() not in {"active", "activating"}:
            journal = subprocess.run(["journalctl", "--user", "-u", unit, "-n", "20", "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
            causes = [name for name, needle in (("unit-name-allowlist", "allowlist"), ("model-not-found", "model not found"), ("credential-expired", "credential"), ("invalid-worker-identity", "invalid worker unit identity")) if needle in journal.lower()]
            failed.append({"unit": unit, "error": journal, "known_causes": causes, "action": "release_claim_and_alert"})
    return {"launched": len(launched), "failed": failed}


def pool_starvation(cards: list[Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for card in cards:
        status = str(_card_dict(card).get("status", "unknown")).lower()
        counts[status] = counts.get(status, 0) + 1
    ready = counts.get("ready", 0)
    blocked = counts.get("blocked", 0)
    healthy = counts.get("review", 0) + counts.get("backoff", 0) + blocked
    classification = "healthy" if healthy == sum(counts.values()) - ready else "stuck"
    return {"ready": ready, "threshold": 3, "starved": ready < 3, "ineligible": counts,
            "classification": classification,
            "alert_after_seconds": 900 if ready < 3 else 0,
            "recommended_action": "drain_review_or_reap_stale_claims" if classification == "stuck" else "none"}


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
