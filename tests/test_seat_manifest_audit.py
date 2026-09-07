from __future__ import annotations

import json
from pathlib import Path

from skcapstone.seat_manifest_audit import audit


SEATS = ("link", "mero", "niobe", "tank", "seraph", "atlas")


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".skcapstone"
    identities = []
    for index, seat in enumerate(SEATS):
        fingerprint = f"{index + 1:040x}".upper()
        identities.append({"fingerprint": fingerprint, "status": "active"})
        agent = home / "agents" / seat
        _write(
            agent / "config" / "seat-role.json",
            {
                "seat": seat,
                "product_scope": ["skcapstone", "skdashboard", "skworld"],
                "mailbox": {"read": True, "acknowledge": True, "write": True},
                "mail_protocol": {
                    "startup_hello": True,
                    "startup_recipient": "all",
                    "read_all_recipient": True,
                    "poll_interval_seconds": 300,
                },
                "model_policy": {"default_route": "sk-codex-mid", "default_model": "gpt-5.6-luna"},
                "card_contract": {
                    "required_card_fields": [
                        "product_scope",
                        "lifecycle_phase",
                        "responsible_seat",
                        "permitted_action",
                        "dependencies",
                        "evidence_schema",
                        "rollback_path",
                        "terminal_decision",
                    ]
                },
                "lifecycle_beat": {"stale_is_observation": True},
            },
        )
        _write(
            agent / "capauth" / "identity" / "profile.json",
            {
                "fqid": f"{seat}@casey.skworld.io",
                "operator": "casey",
                "operator_fingerprint": "A" * 40,
                "key_info": {"fingerprint": fingerprint},
            },
        )
        for relative in (
            "capauth/identity/public.asc",
            "capauth/identity/operator-attestation.json",
            "identity/identity.json",
        ):
            path = agent / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        startup = agent / "config" / "lifecycle-startup.md"
        startup.parent.mkdir(parents=True, exist_ok=True)
        startup.write_text("hello", encoding="utf-8")
    _write(home / "capauth" / "estate.json", {"identities": identities})
    return home


def test_all_lifecycle_seats_pass_manifest_audit(tmp_path: Path) -> None:
    assert audit(_home(tmp_path)) == []


def test_manifest_audit_catches_model_and_estate_drift(tmp_path: Path) -> None:
    home = _home(tmp_path)
    role_path = home / "agents" / "tank" / "config" / "seat-role.json"
    role = json.loads(role_path.read_text(encoding="utf-8"))
    role["model_policy"]["default_model"] = "wrong-model"
    role_path.write_text(json.dumps(role), encoding="utf-8")
    estate_path = home / "capauth" / "estate.json"
    estate = json.loads(estate_path.read_text(encoding="utf-8"))
    estate["identities"][4]["status"] = "revoked"
    estate_path.write_text(json.dumps(estate), encoding="utf-8")
    findings = audit(home)
    assert any(f.seat == "tank" and f.field == "model_policy" for f in findings)
    assert any(f.seat == "seraph" and f.field == "estate" for f in findings)


def test_manifest_audit_catches_duplicate_identity(tmp_path: Path) -> None:
    home = _home(tmp_path)
    link = home / "agents" / "link" / "capauth" / "identity" / "profile.json"
    mero = home / "agents" / "mero" / "capauth" / "identity" / "profile.json"
    link_profile = json.loads(link.read_text(encoding="utf-8"))
    mero_profile = json.loads(mero.read_text(encoding="utf-8"))
    mero_profile["key_info"]["fingerprint"] = link_profile["key_info"]["fingerprint"]
    mero.write_text(json.dumps(mero_profile), encoding="utf-8")
    findings = audit(home)
    assert any(f.seat == "mero" and f.field == "fingerprint" for f in findings)
