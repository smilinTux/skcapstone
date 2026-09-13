"""Read-only validation for the SK software-lifecycle seat manifests.

The canonical document shape is the one ``skcapstone.lifecycle_seats``
converges into ``config/seat-role.json`` (schema ``sk.lifecycle-seat/v1``,
backed by ``data/lifecycle-seat-profiles.json``). This audit validates the
manifests against that documented contract and fails closed whenever a
genuinely required mailbox, logical route, or lifecycle field is missing.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SEATS = ("link", "mero", "niobe", "tank", "seraph", "atlas")
PRODUCT_SCOPE = ["skcapstone", "skdashboard", "skworld"]
SEAT_SCHEMA = "sk.lifecycle-seat/v1"
DEFAULT_MODEL_ROUTE = "sk-codex-mid"
DEFAULT_MODEL_PROFILE = "gpt-5.6-luna"
MODEL_ESCALATION_POLICY = {"scope": "card", "mode": "opt_in", "automatic": False}
SAFE_RETIREMENT = {
    "abandon_only_after_exact_process_generation_is_dead": True,
    "oneshot": True,
    "overlap_action": "record_noop",
    "persistent_worker_after_cycle": False,
    "preserve_receipts": True,
}
MAILBOX_CAPABILITIES = {"read": True, "acknowledge": True, "write": True}


@dataclass(frozen=True)
class Finding:
    seat: str
    field: str
    message: str


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _require_file(home: Path, seat: str, relative: str, findings: list[Finding]) -> None:
    if not (home / "agents" / seat / relative).is_file():
        findings.append(Finding(seat, relative, "required file is missing"))


def _check_mailbox(seat: str, role: Mapping[str, Any], findings: list[Finding]) -> None:
    mailbox = role.get("mailbox")
    if mailbox != MAILBOX_CAPABILITIES:
        findings.append(Finding(seat, "mailbox", "must allow read, acknowledge, and write"))
    protocol = role.get("mail_protocol")
    if not isinstance(protocol, dict):
        findings.append(Finding(seat, "mail_protocol", "is missing"))
        return
    if protocol.get("startup_hello") is not True or protocol.get("startup_recipient") != "all":
        findings.append(Finding(seat, "mail_protocol.startup", "must send a startup hello to all"))
    if protocol.get("read_direct_and_all") is not True:
        findings.append(
            Finding(
                seat,
                "mail_protocol.read_direct_and_all",
                "must read direct and all-recipient views",
            )
        )
    if (
        not isinstance(protocol.get("poll_interval_seconds"), int)
        or protocol["poll_interval_seconds"] > 300
    ):
        findings.append(
            Finding(seat, "mail_protocol.poll_interval_seconds", "must be at most 300 seconds")
        )


def _check_logical_route(seat: str, role: Mapping[str, Any], findings: list[Finding]) -> None:
    if (
        role.get("model_route") != DEFAULT_MODEL_ROUTE
        or role.get("model_profile") != DEFAULT_MODEL_PROFILE
    ):
        findings.append(
            Finding(seat, "model_route", "default must be Luna medium through sk-codex-mid")
        )
    if role.get("model_escalation_policy") != MODEL_ESCALATION_POLICY:
        findings.append(Finding(seat, "model_escalation_policy", "must be card-scoped and opt-in"))


def _check_lifecycle_fields(seat: str, role: Mapping[str, Any], findings: list[Finding]) -> None:
    if role.get("schema") != SEAT_SCHEMA:
        findings.append(Finding(seat, "schema", "must be the canonical lifecycle seat schema"))
    if not str(role.get("activation_state") or ""):
        findings.append(Finding(seat, "activation_state", "must name the seat activation state"))
    if role.get("safe_retirement") != SAFE_RETIREMENT:
        findings.append(
            Finding(seat, "safe_retirement", "must preserve receipts and retire safely")
        )
    beat = role.get("lifecycle_beat")
    if not isinstance(beat, dict) or beat.get("stale_is_observation") is not True:
        findings.append(Finding(seat, "lifecycle_beat", "stale beats must remain observations"))


def _check_seat(home: Path, seat: str, estate: Sequence[Mapping[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    agent = home / "agents" / seat
    role_path = agent / "config" / "seat-role.json"
    profile_path = agent / "capauth" / "identity" / "profile.json"
    role = _read_json(role_path)
    profile = _read_json(profile_path)
    if role is None:
        findings.append(Finding(seat, "config/seat-role.json", "invalid or unreadable JSON"))
        return findings
    if profile is None:
        findings.append(
            Finding(seat, "capauth/identity/profile.json", "invalid or unreadable JSON")
        )
        return findings

    if role.get("seat") != seat:
        findings.append(Finding(seat, "seat", "manifest seat does not match directory"))
    if role.get("product_scope") != PRODUCT_SCOPE:
        findings.append(
            Finding(seat, "product_scope", "must cover SKCapstone, SKDashboard, and SKWorld")
        )
    if profile.get("fqid") != f"{seat}@casey.skworld.io":
        findings.append(Finding(seat, "fqid", "must be the seat identity at casey.skworld.io"))
    if profile.get("operator") != "casey":
        findings.append(Finding(seat, "operator", "must be Casey"))
    key_info = profile.get("key_info")
    fingerprint = str(key_info.get("fingerprint") if isinstance(key_info, dict) else "")
    if len(fingerprint) != 40:
        findings.append(Finding(seat, "fingerprint", "must be a full identity fingerprint"))
    if not str(profile.get("operator_fingerprint") or ""):
        findings.append(
            Finding(seat, "operator_fingerprint", "operator attestation fingerprint is missing")
        )

    _check_mailbox(seat, role, findings)
    _check_logical_route(seat, role, findings)
    _check_lifecycle_fields(seat, role, findings)
    for relative in (
        "capauth/identity/public.asc",
        "capauth/identity/operator-attestation.json",
        "identity/identity.json",
        "config/lifecycle-startup.md",
    ):
        _require_file(home, seat, relative, findings)

    if fingerprint and not any(
        entry.get("fingerprint") == fingerprint and entry.get("status") == "active"
        for entry in estate
    ):
        findings.append(
            Finding(seat, "estate", "fingerprint is not backed by an active estate entry")
        )
    return findings


def audit(home: Path, seats: Sequence[str] = SEATS) -> list[Finding]:
    """Return drift findings without changing any file or runtime state."""

    estate_doc = _read_json(home / "capauth" / "estate.json") or {}
    estate = estate_doc.get("identities", [])
    if not isinstance(estate, list):
        estate = []
    findings: list[Finding] = []
    fingerprints: dict[str, str] = {}
    for seat in seats:
        findings.extend(_check_seat(home, seat, estate))
        profile = (
            _read_json(home / "agents" / seat / "capauth" / "identity" / "profile.json") or {}
        )
        key_info = profile.get("key_info")
        fingerprint = str(key_info.get("fingerprint") if isinstance(key_info, dict) else "")
        if fingerprint:
            previous = fingerprints.get(fingerprint)
            if previous and previous != seat:
                findings.append(
                    Finding(seat, "fingerprint", f"duplicates lifecycle identity {previous}")
                )
            fingerprints[fingerprint] = seat
    return findings


def report(home: Path, seats: Sequence[str] = SEATS) -> dict[str, Any]:
    findings = audit(home, seats)
    return {
        "healthy": not findings,
        "home": str(home),
        "seats": list(seats),
        "findings": [asdict(finding) for finding in findings],
    }
