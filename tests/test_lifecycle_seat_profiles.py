"""Contract tests for the six lifecycle seats and Jarvis exclusion."""

import json
from pathlib import Path

from skcapstone.lifecycle_seats import (
    LIFECYCLE_SEATS,
    PROFILE_FILENAME,
    STARTUP_FILENAME,
    converge_lifecycle_seats,
    load_lifecycle_seat_profiles,
    load_seat_control_plane,
    rollback_lifecycle_seats,
)


def test_all_six_profiles_share_the_runtime_contract() -> None:
    value = load_lifecycle_seat_profiles()
    assert set(value["seats"]) == LIFECYCLE_SEATS
    assert value["product_scope"] == ["skcapstone", "skdashboard", "skworld"]
    assert value["repositories"] == [
        "smilinTux/skcapstone",
        "smilinTux/skdashboard",
        "smilinTux/skworld",
        "smilinTux/sk-standards",
    ]
    assert value["default_model_route"] == "sk-codex-mid"
    assert value["default_model_profile"] == "gpt-5.6-luna"
    assert value["model_escalation_policy"] == {
        "scope": "card",
        "mode": "opt_in",
        "automatic": False,
    }
    assert value["active_host"] == "chiap08"
    assert value["mail"] == {
        "startup_hello_recipient": "all",
        "poll_direct_and_all": True,
        "write": True,
        "automatic_ack": False,
        "mail_is_authority": False,
        "help_terms": ["help", "handoff", "dependency", "reviewer conflict"],
    }
    assert value["safe_retirement"]["persistent_worker_after_cycle"] is False
    for seat, profile in value["seats"].items():
        assert profile["identity"] == seat
        assert profile["activation_state"] == "active_bounded"
        assert profile["card_label"] == f"seat-{seat}"
        assert profile["cadence_seconds"] == 300
        assert profile["timeout_seconds"] <= 300
        assert profile["owns"]
        assert profile["denies"]


def test_jarvis_is_emergency_only_and_keeps_requested_tools() -> None:
    jarvis = load_lifecycle_seat_profiles()["jarvis"]
    assert jarvis["recurring_lifecycle"] is False
    assert jarvis["requires_casey_direction"] is True
    assert jarvis["emergency_gateway"] == ("skcapstone.jarvis_emergency.JarvisEmergencyGateway")
    assert jarvis["direction_schema"] == "atlas-authorization/v1"
    assert jarvis["direction_scope"] == "skcapstone,skdashboard,skworld"
    assert set(jarvis["emergency_tools"]) == {
        "card_creation",
        "card_claim",
        "card_completion",
        "fleet",
        "merge",
        "deployment",
        "release",
        "verification",
        "actuation",
    }


def test_source_placement_matches_the_six_profiles() -> None:
    root = Path(__file__).parents[1]
    placement = json.loads((root / "scripts/fleet/seat-placement.json").read_text())
    assert set(placement["seats"]) == LIFECYCLE_SEATS
    assert all(hosts == ["chiap08"] for hosts in placement["seats"].values())


def test_packaged_and_source_units_are_byte_identical() -> None:
    root = Path(__file__).parents[1]
    for seat in ("tank", "atlas"):
        for suffix in ("service", "timer"):
            name = f"skfleet-{seat}.{suffix}"
            assert (root / "systemd" / name).read_bytes() == (
                root / "src/skcapstone/data/systemd" / name
            ).read_bytes()


def test_mero_profile_cadence_matches_its_five_minute_timer() -> None:
    root = Path(__file__).parents[1]
    profile = load_lifecycle_seat_profiles()["seats"]["mero"]
    timer = (root / "systemd/skfleet-mero.timer").read_text()
    assert profile["cadence_seconds"] == 300
    assert "OnUnitActiveSec=5min" in timer


def test_control_plane_and_every_timer_have_exact_six_seat_five_minute_contract() -> None:
    root = Path(__file__).parents[1]
    control = load_seat_control_plane()
    assert set(control["seats"]) == LIFECYCLE_SEATS
    assert all(hosts == ["chiap08"] for hosts in control["seats"].values())
    for seat in LIFECYCLE_SEATS:
        timer_name = "skfleet-niobe-live.timer" if seat == "niobe" else f"skfleet-{seat}.timer"
        timer = (root / "systemd" / timer_name).read_text()
        assert "every five minutes" in timer.lower()
        assert "15min" not in timer


def test_profile_convergence_is_exact_idempotent_and_rollback_safe(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rollback = tmp_path / "rollback"
    prior = home / "agents/link" / PROFILE_FILENAME
    for seat in LIFECYCLE_SEATS:
        identity = home / "agents" / seat / "identity/identity.json"
        identity.parent.mkdir(parents=True)
        identity.write_text(json.dumps({"name": seat.title()}))
    prior.parent.mkdir(parents=True)
    prior.write_text("prior\n")

    first = converge_lifecycle_seats(home, rollback)
    installed = {
        seat: (home / "agents" / seat / PROFILE_FILENAME).read_bytes() for seat in LIFECYCLE_SEATS
    }
    second = converge_lifecycle_seats(home, tmp_path / "rollback-second")
    assert first["schema"] == second["schema"]
    assert set(installed) == LIFECYCLE_SEATS
    assert all(b'"poll_interval_seconds":300' in value for value in installed.values())
    assert all(b'"read_direct_and_all":true' in value for value in installed.values())
    assert all(b'"look_for_help_and_handoffs":true' in value for value in installed.values())
    assert all(b'"interval_seconds":300' in value for value in installed.values())
    assert all(b'"model_route":"sk-codex-mid"' in value for value in installed.values())
    assert all(b'"model_profile":"gpt-5.6-luna"' in value for value in installed.values())
    assert all(
        b'"model_escalation_policy":{"automatic":false,"mode":"opt_in","scope":"card"}' in value
        for value in installed.values()
    )
    assert all((home / "agents" / seat / STARTUP_FILENAME).is_file() for seat in LIFECYCLE_SEATS)
    assert load_seat_control_plane() == json.loads(
        (home / "coordination/seat-control-plane.json").read_text()
    )

    rollback_lifecycle_seats(home, rollback)
    assert prior.read_text() == "prior\n"
    for seat in LIFECYCLE_SEATS - {"link"}:
        assert not (home / "agents" / seat / PROFILE_FILENAME).exists()
    assert all(
        not (home / "agents" / seat / STARTUP_FILENAME).exists() for seat in LIFECYCLE_SEATS
    )
    assert not (home / "coordination/seat-control-plane.json").exists()


def test_profile_convergence_refuses_missing_or_mismatched_identity(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for seat in LIFECYCLE_SEATS:
        identity = home / "agents" / seat / "identity/identity.json"
        identity.parent.mkdir(parents=True)
        identity.write_text(json.dumps({"name": seat.title()}))
    (home / "agents/tank/identity/identity.json").write_text(json.dumps({"name": "Jarvis"}))
    try:
        converge_lifecycle_seats(home, tmp_path / "rollback")
    except ValueError as exc:
        assert "identity does not match" in str(exc)
    else:
        raise AssertionError("mismatched lifecycle identity accepted")
