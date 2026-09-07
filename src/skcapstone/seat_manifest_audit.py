"""Read-only validation for the SK software-lifecycle seat manifests."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SEATS = ("link", "mero", "niobe", "tank", "seraph", "atlas")
PRODUCT_SCOPE = ["skcapstone", "skdashboard", "skworld"]
REQUIRED_CARD_FIELDS = {
    "product_scope",
    "lifecycle_phase",
    "responsible_seat",
    "permitted_action",
    "dependencies",
    "evidence_schema",
    "rollback_path",
    "terminal_decision",
}


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
        findings.append(Finding(seat, "capauth/identity/profile.json", "invalid or unreadable JSON"))
        return findings

    if role.get("seat") != seat:
        findings.append(Finding(seat, "seat", "manifest seat does not match directory"))
    if role.get("product_scope") != PRODUCT_SCOPE:
        findings.append(Finding(seat, "product_scope", "must cover SKCapstone, SKDashboard, and SKWorld"))
    if profile.get("fqid") != f"{seat}@casey.skworld.io":
        findings.append(Finding(seat, "fqid", "must be the seat identity at casey.skworld.io"))
    if profile.get("operator") != "casey":
        findings.append(Finding(seat, "operator", "must be Casey"))
    key_info = profile.get("key_info")
    fingerprint = str(key_info.get("fingerprint") if isinstance(key_info, dict) else "")
    if len(fingerprint) != 40:
        findings.append(Finding(seat, "fingerprint", "must be a full identity fingerprint"))
    if not str(profile.get("operator_fingerprint") or ""):
        findings.append(Finding(seat, "operator_fingerprint", "operator attestation fingerprint is missing"))

    mailbox = role.get("mailbox")
    if mailbox != {"read": True, "acknowledge": True, "write": True}:
        findings.append(Finding(seat, "mailbox", "must allow read, acknowledge, and write"))
    protocol = role.get("mail_protocol")
    if not isinstance(protocol, dict):
        findings.append(Finding(seat, "mail_protocol", "is missing"))
    else:
        if protocol.get("startup_hello") is not True or protocol.get("startup_recipient") != "all":
            findings.append(Finding(seat, "mail_protocol.startup", "must send a startup hello to all"))
        if protocol.get("read_all_recipient") is not True:
            findings.append(Finding(seat, "mail_protocol.read_all", "must read the all-recipient view"))
        if not isinstance(protocol.get("poll_interval_seconds"), int) or protocol["poll_interval_seconds"] > 300:
            findings.append(Finding(seat, "mail_protocol.poll_interval_seconds", "must be at most 300 seconds"))
    model = role.get("model_policy")
    if not isinstance(model, dict) or model.get("default_route") != "sk-codex-mid" or model.get("default_model") != "gpt-5.6-luna":
        findings.append(Finding(seat, "model_policy", "default must be Luna medium through sk-codex-mid"))
    card = role.get("card_contract")
    if not isinstance(card, dict) or not REQUIRED_CARD_FIELDS.issubset(set(card.get("required_card_fields", []))):
        findings.append(Finding(seat, "card_contract", "required lifecycle fields are incomplete"))
    beat = role.get("lifecycle_beat")
    if not isinstance(beat, dict) or beat.get("stale_is_observation") is not True:
        findings.append(Finding(seat, "lifecycle_beat", "stale beats must remain observations"))
    for relative in (
        "capauth/identity/public.asc",
        "capauth/identity/operator-attestation.json",
        "identity/identity.json",
        "config/lifecycle-startup.md",
    ):
        _require_file(home, seat, relative, findings)

    if fingerprint and not any(entry.get("fingerprint") == fingerprint and entry.get("status") == "active" for entry in estate):
        findings.append(Finding(seat, "estate", "fingerprint is not backed by an active estate entry"))
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
        profile = _read_json(home / "agents" / seat / "capauth" / "identity" / "profile.json") or {}
        key_info = profile.get("key_info")
        fingerprint = str(key_info.get("fingerprint") if isinstance(key_info, dict) else "")
        if fingerprint:
            previous = fingerprints.get(fingerprint)
            if previous and previous != seat:
                findings.append(Finding(seat, "fingerprint", f"duplicates lifecycle identity {previous}"))
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
