"""Regression tests for the canonical lifecycle seat manifest audit.

The fixture mirrors the exact document ``skcapstone.lifecycle_seats``
converges into ``config/seat-role.json`` (schema ``sk.lifecycle-seat/v1``),
so a healthy canonical estate produces no findings, while genuinely missing
mailbox, logical route, or lifecycle requirements still fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.seat_manifest_audit import audit

SEATS = ("link", "mero", "niobe", "tank", "seraph", "atlas")


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _canonical_role(seat: str) -> dict[str, object]:
    return {
        "schema": "sk.lifecycle-seat/v1",
        "seat": seat,
        "model_route": "sk-codex-mid",
        "model_profile": "gpt-5.6-luna",
        "model_escalation_policy": {"scope": "card", "mode": "opt_in", "automatic": False},
        "product_scope": ["skcapstone", "skdashboard", "skworld"],
        "role": "integrator",
        "activation_state": "active_bounded",
        "owns": ["pr_triage"],
        "does_not_own": ["merge", "deployment"],
        "mailbox": {"read": True, "acknowledge": True, "write": True},
        "mail_protocol": {
            "startup_hello": True,
            "startup_recipient": "all",
            "poll_interval_seconds": 300,
            "read_direct_and_all": True,
            "look_for_help_and_handoffs": True,
            "automatic_ack": False,
            "mail_is_authority": False,
        },
        "lifecycle_beat": {
            "enabled": True,
            "mode": "per_cycle",
            "interval_seconds": 300,
            "includes": ["seat", "host", "mailbox_poll_at", "status"],
            "stale_is_observation": True,
        },
        "safe_retirement": {
            "abandon_only_after_exact_process_generation_is_dead": True,
            "oneshot": True,
            "overlap_action": "record_noop",
            "persistent_worker_after_cycle": False,
            "preserve_receipts": True,
        },
        "card_label": f"seat-{seat}",
        "timeout_seconds": 300,
    }


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".skcapstone"
    identities = []
    for index, seat in enumerate(SEATS):
        fingerprint = f"{index + 1:040x}".upper()
        identities.append({"fingerprint": fingerprint, "status": "active"})
        agent = home / "agents" / seat
        _write(agent / "config" / "seat-role.json", _canonical_role(seat))
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


def _role(home: Path, seat: str) -> dict[str, object]:
    role_path = home / "agents" / seat / "config" / "seat-role.json"
    return json.loads(role_path.read_text(encoding="utf-8"))


def _save_role(home: Path, seat: str, role: dict[str, object]) -> None:
    role_path = home / "agents" / seat / "config" / "seat-role.json"
    role_path.write_text(json.dumps(role), encoding="utf-8")


def test_all_lifecycle_seats_pass_manifest_audit(tmp_path: Path) -> None:
    assert audit(_home(tmp_path)) == []


def test_audit_catches_model_and_estate_drift(tmp_path: Path) -> None:
    home = _home(tmp_path)
    role = _role(home, "tank")
    role["model_profile"] = "wrong-model"
    _save_role(home, "tank", role)
    estate_path = home / "capauth" / "estate.json"
    estate = json.loads(estate_path.read_text(encoding="utf-8"))
    estate["identities"][4]["status"] = "revoked"
    estate_path.write_text(json.dumps(estate), encoding="utf-8")
    findings = audit(home)
    assert any(f.seat == "tank" and f.field == "model_route" for f in findings)
    assert any(f.seat == "seraph" and f.field == "estate" for f in findings)


def test_audit_catches_duplicate_identity(tmp_path: Path) -> None:
    home = _home(tmp_path)
    link = home / "agents" / "link" / "capauth" / "identity" / "profile.json"
    mero = home / "agents" / "mero" / "capauth" / "identity" / "profile.json"
    link_profile = json.loads(link.read_text(encoding="utf-8"))
    mero_profile = json.loads(mero.read_text(encoding="utf-8"))
    mero_profile["key_info"]["fingerprint"] = link_profile["key_info"]["fingerprint"]
    mero.write_text(json.dumps(mero_profile), encoding="utf-8")
    findings = audit(home)
    assert any(f.seat == "mero" and f.field == "fingerprint" for f in findings)


def test_audit_accepts_canonical_read_direct_and_all_mailbox(tmp_path: Path) -> None:
    """The canonical mailbox field is ``read_direct_and_all`` (card 353e53d2)."""
    home = _home(tmp_path)
    role = _role(home, "link")
    assert role["mail_protocol"]["read_direct_and_all"] is True
    assert audit(home) == []


def test_audit_fails_closed_when_read_direct_and_all_missing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    role = _role(home, "link")
    del role["mail_protocol"]["read_direct_and_all"]
    _save_role(home, "link", role)
    findings = audit(home)
    assert any(
        f.seat == "link" and f.field == "mail_protocol.read_direct_and_all" for f in findings
    )


def test_audit_fails_closed_when_logical_route_missing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    role = _role(home, "niobe")
    del role["model_route"]
    del role["model_profile"]
    _save_role(home, "niobe", role)
    findings = audit(home)
    assert any(f.seat == "niobe" and f.field == "model_route" for f in findings)


def test_audit_fails_closed_when_lifecycle_fields_missing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    role = _role(home, "seraph")
    del role["schema"]
    del role["activation_state"]
    del role["safe_retirement"]
    _save_role(home, "seraph", role)
    fields = {f.field for f in audit(home) if f.seat == "seraph"}
    assert {"schema", "activation_state", "safe_retirement"} <= fields


def test_audit_flags_uncontrolled_model_escalation(tmp_path: Path) -> None:
    home = _home(tmp_path)
    role = _role(home, "atlas")
    role["model_escalation_policy"] = {"scope": "fleet", "mode": "automatic", "automatic": True}
    _save_role(home, "atlas", role)
    findings = audit(home)
    assert any(f.seat == "atlas" and f.field == "model_escalation_policy" for f in findings)
