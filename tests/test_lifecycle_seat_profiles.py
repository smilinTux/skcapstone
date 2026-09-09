"""Contract tests for the six lifecycle seats and Jarvis exclusion."""

from pathlib import Path

from skcapstone.lifecycle_seats import LIFECYCLE_SEATS, load_lifecycle_seat_profiles


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
        assert profile["cadence_seconds"] > 0
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
    import json

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
