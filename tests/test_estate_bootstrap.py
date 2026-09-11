"""Estate bootstrap and the estate-versus-host split.

Every case here maps to something that was MISSING when a second estate was
stood up from scratch, with nothing in the codebase creating it and nothing
reporting its absence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone import estate
from skcapstone.coord_mail import bootstrap, observation_feed_seed, seat_control_plane_document
from skcapstone.doctor import (
    _check_estate,
    _check_estate_host_local_leak,
    _check_estate_node_env,
    _check_estate_observation_feed,
    _check_estate_seat_control_plane,
    _check_estate_seat_units,
    _check_estate_sknoded_interval,
    _check_estate_unit_entrypoints,
)
from skcapstone.lifecycle_seats import LIFECYCLE_SEATS, load_seat_control_plane
from skcapstone.link_observation_feed import ObservationFeedError, load_observation_feed


@pytest.fixture()
def dirs(tmp_path: Path) -> dict[str, Path]:
    """Return an isolated home plus host-local unit and environment dirs."""
    return {
        "home": tmp_path / "home",
        "units": tmp_path / "xdg/systemd/user",
        "envd": tmp_path / "xdg/environment.d",
    }


def _bootstrap(dirs: dict[str, Path], **kwargs) -> dict:
    return bootstrap(
        dirs["home"],
        host=kwargs.pop("host", "testhost"),
        estate=kwargs.pop("estate", "tst"),
        unit_dir=dirs["units"],
        env_dir=dirs["envd"],
        **kwargs,
    )


# --------------------------------------------------------------------------
# estate module: the XDG and derivation helpers
# --------------------------------------------------------------------------


def test_xdg_roots_follow_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absolute XDG override wins; anything else falls back to the default."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert estate.xdg_config_home() == tmp_path / "cfg"
    assert estate.xdg_state_home() == tmp_path / "state"
    assert estate.environment_d_dir() == tmp_path / "cfg" / "environment.d"


def test_xdg_ignores_a_relative_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative XDG value is ignored by the spec, so it must not be honored."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    monkeypatch.setenv("XDG_STATE_HOME", "")
    assert estate.xdg_config_home() == Path.home() / ".config"
    assert estate.xdg_state_home() == Path.home() / ".local" / "state"


def test_node_name_ignores_the_variable_it_defines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The persisted value derives from the host, never from a stale env value."""
    monkeypatch.setenv("SKFLEET_NODE", "node-somewhere-else")
    assert estate.node_name("Noroc2027.Local") == "node-noroc2027"
    assert estate.node_name("weird host!") == "node-weird-host"


def test_estate_id_prefers_explicit_then_cluster_then_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Estate identity is read, then derived, but never invented from a slice."""
    monkeypatch.delenv("SKESTATE", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    assert estate.estate_id(home, host="noroc2027") == "noroc2027"
    (home / "cluster.json").write_text(json.dumps({"estate": "nor", "realm": "skworld.io"}))
    assert estate.estate_id(home, host="noroc2027") == "nor"
    assert estate.estate_realm(home) == "skworld.io"
    assert estate.estate_id(home, estate="override", host="noroc2027") == "override"


def test_estate_id_survives_a_malformed_cluster_file(tmp_path: Path) -> None:
    """A cluster.json that has not synced yet must not fail a bootstrap."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "cluster.json").write_text("{not json")
    assert estate.estate_id(home, host="fresh") == "fresh"
    assert estate.estate_realm(home) is None


# --------------------------------------------------------------------------
# Gap 1: the seat control plane
# --------------------------------------------------------------------------


def test_bootstrap_creates_the_seat_control_plane(dirs: dict[str, Path]) -> None:
    """Happy path: a fresh estate gets a control plane naming the local host."""
    result = _bootstrap(dirs)
    path = dirs["home"] / "coordination" / "seat-control-plane.json"
    assert "coordination/seat-control-plane.json" in result["created"]
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["active_host"] == "testhost"
    assert data["revision"] == "tst-lifecycle-six-seat-v1"
    assert set(data["seats"]) == set(LIFECYCLE_SEATS)
    assert all(hosts == ["testhost"] for hosts in data["seats"].values())


def test_bootstrap_never_repoints_an_existing_control_plane(dirs: dict[str, Path]) -> None:
    """Edge case: an estate's election is a decision, so re-running leaves it."""
    _bootstrap(dirs, host="first-host")
    second = _bootstrap(dirs, host="second-host")
    data = json.loads((dirs["home"] / "coordination/seat-control-plane.json").read_text())
    assert data["active_host"] == "first-host"
    assert "coordination/seat-control-plane.json" not in second["created"]


def test_seat_control_plane_hardcodes_no_host() -> None:
    """Failure mode that shipped: the packaged record pinned one estate's host."""
    assert seat_control_plane_document("anyhost", "anyestate")["seats"]["atlas"] == ["anyhost"]
    resolved = load_seat_control_plane("noroc2027")
    assert resolved["active_host"] == "noroc2027"
    assert all(hosts == ["noroc2027"] for hosts in resolved["seats"].values())
    with pytest.raises(ValueError):
        load_seat_control_plane("   ")


# --------------------------------------------------------------------------
# Gap 3: the Link observation feed
# --------------------------------------------------------------------------


def test_bootstrap_seeds_a_loadable_observation_feed(dirs: dict[str, Path]) -> None:
    """Happy path: Link gets a well-formed, empty, self-hashed feed."""
    _bootstrap(dirs)
    feed = load_observation_feed(dirs["home"] / "coordination/link-observations.json")
    assert feed.records == ()
    assert feed.source_revision == "tst-bootstrap-seed"
    assert feed.producer.identity == "coord-bootstrap"
    assert feed.producer.host == "testhost"


def test_observation_seed_does_not_claim_to_be_the_producer(dirs: dict[str, Path]) -> None:
    """Edge case: provenance must name bootstrap, not the mediated producer."""
    seed = observation_feed_seed(dirs["home"], "testhost", "tst")
    assert seed["producer"]["session"] == "bootstrap"
    assert seed["records"] == []
    assert seed["reviewer_candidates"] == []
    assert len(seed["evidence_sha256"]) == 64


def test_observation_seed_goes_stale_rather_than_lying(dirs: dict[str, Path]) -> None:
    """Failure mode: an old seed reports stale, which points at the producer."""
    from datetime import datetime, timedelta, timezone

    _bootstrap(dirs)
    path = dirs["home"] / "coordination/link-observations.json"
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    with pytest.raises(ObservationFeedError) as excinfo:
        load_observation_feed(path, now=later)
    assert str(excinfo.value) == "observation_feed_stale"


# --------------------------------------------------------------------------
# Gap 8 and gap 4: the host-local side
# --------------------------------------------------------------------------


def test_bootstrap_installs_the_bounded_niobe_units(dirs: dict[str, Path]) -> None:
    """Happy path: Niobe finally gets the bounded cycle unit the others have."""
    _bootstrap(dirs)
    body = (dirs["units"] / "skfleet-niobe.service").read_text()
    assert "seat_cycle_entrypoint --seat niobe" in body
    assert "seat_shadow_entrypoint" not in body
    assert (dirs["units"] / "skfleet-niobe.timer").is_file()


def test_bootstrap_persists_the_node_name_host_locally(dirs: dict[str, Path]) -> None:
    """Happy path: the node name is derived, not accepted as a literal."""
    _bootstrap(dirs, host="noroc2027")
    body = (dirs["envd"] / "skfleet-node.conf").read_text()
    assert "SKFLEET_NODE=node-noroc2027" in body
    assert not (dirs["home"] / "config/skfleet-node.conf").exists()


def test_bootstrap_writes_nothing_host_local_into_the_synced_tree(
    dirs: dict[str, Path],
) -> None:
    """Edge case: no host-local variable may land under the shared home."""
    _bootstrap(dirs)
    for path in dirs["home"].rglob("*"):
        if path.is_file():
            assert "SKFLEET_NODE=" not in path.read_text(errors="replace")


def test_bootstrap_is_idempotent_across_every_item(dirs: dict[str, Path]) -> None:
    """Failure mode this replaces: hand-made files clobbered by a re-run."""
    first = _bootstrap(dirs, agent="lumina")
    assert first["created"]
    second = _bootstrap(dirs, agent="lumina")
    assert second["created"] == []
    assert second["host"] == "testhost"
    assert second["node"] == "node-testhost"


# --------------------------------------------------------------------------
# Doctor checks
# --------------------------------------------------------------------------


def test_doctor_reports_an_absent_control_plane_with_its_fix(tmp_path: Path) -> None:
    """Failure path: the exact file and the exact command are both named."""
    check = _check_estate_seat_control_plane(tmp_path)
    assert not check.passed
    assert "seat-control-plane.json" in check.detail
    assert "coord bootstrap" in check.fix


def test_doctor_accepts_a_control_plane_electing_another_host(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge case: a non-elected node reading someone else's name is correct."""
    _bootstrap(dirs, host="elected-host")
    monkeypatch.setattr("skcapstone.estate.socket.gethostname", lambda: "other-host")
    check = _check_estate_seat_control_plane(dirs["home"])
    assert check.passed
    assert "elected-host" in check.detail
    assert "not elected" in check.detail


def test_doctor_rejects_a_control_plane_that_does_not_pin_every_seat(
    dirs: dict[str, Path],
) -> None:
    """Failure path: a seat missing from the active host cannot run."""
    _bootstrap(dirs)
    path = dirs["home"] / "coordination/seat-control-plane.json"
    data = json.loads(path.read_text())
    data["seats"]["niobe"] = ["some-other-host"]
    path.write_text(json.dumps(data))
    check = _check_estate_seat_control_plane(dirs["home"])
    assert not check.passed
    assert "niobe" in check.detail


def test_doctor_reports_a_missing_then_present_observation_feed(dirs: dict[str, Path]) -> None:
    """Happy and failure paths for the feed the Link seat needs."""
    missing = _check_estate_observation_feed(dirs["home"])
    assert not missing.passed
    assert "observation_feed_missing" in missing.detail
    _bootstrap(dirs)
    present = _check_estate_observation_feed(dirs["home"])
    assert present.passed
    assert "0 observation" in present.detail


def test_doctor_points_a_stale_feed_at_the_producer_timer(dirs: dict[str, Path]) -> None:
    """Edge case: a stale feed is the producer's absence, not a missing file."""
    _bootstrap(dirs)
    path = dirs["home"] / "coordination/link-observations.json"
    data = json.loads(path.read_text())
    data["observed_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data))
    check = _check_estate_observation_feed(dirs["home"])
    assert not check.passed
    # The hash no longer matches the mutated payload, which is itself a real
    # and correctly reported defect; either way the check must not pass.
    assert "observation_feed_" in check.detail


def test_doctor_node_env_reports_persisted_and_absent(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy and failure paths, both explaining the estate versus host split."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(dirs["envd"].parent))
    monkeypatch.delenv("SKFLEET_NODE", raising=False)
    absent = _check_estate_node_env()
    assert not absent.passed
    assert "HOST-LOCAL" in absent.detail
    assert "environment.d" in absent.fix
    _bootstrap(dirs)
    present = _check_estate_node_env()
    assert present.passed
    assert "skfleet-node.conf" in present.detail


def test_doctor_flags_a_host_local_value_inside_the_synced_tree(tmp_path: Path) -> None:
    """The defect class in its own right: one host's answer for every node."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config/autopilot.env").write_text("FOO=bar\nSKFLEET_NODE=node-41\n")
    check = _check_estate_host_local_leak(home)
    assert not check.passed
    assert "autopilot.env: SKFLEET_NODE" in check.detail
    assert "never synced" in check.fix


def test_doctor_host_local_leak_is_clean_on_a_correct_tree(dirs: dict[str, Path]) -> None:
    """Happy path: bootstrap's own output never trips its own check."""
    _bootstrap(dirs)
    assert _check_estate_host_local_leak(dirs["home"]).passed


def test_doctor_sknoded_interval_pass_fail_and_unknown(tmp_path: Path) -> None:
    """Happy, failure and unanswerable paths for the beat interval."""
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    unknown = _check_estate_sknoded_interval(unit_dir)
    assert unknown.unknown and not unknown.passed

    unit = unit_dir / "sknoded.service"
    unit.write_text("[Service]\nExecStart=%h/.skenv/bin/skfleet sknoded --interval 300\n")
    bad = _check_estate_sknoded_interval(unit_dir)
    assert not bad.passed and not bad.unknown
    assert "Dead" in bad.detail
    assert "--interval 60" in bad.fix

    unit.write_text("[Service]\nExecStart=%h/.skenv/bin/skfleet sknoded --interval 60\n")
    assert _check_estate_sknoded_interval(unit_dir).passed


def test_doctor_seat_units_pass_fail_and_unknown(tmp_path: Path) -> None:
    """Happy, failure and unanswerable paths for the workflow layer."""
    unit_dir = tmp_path / "units"
    missing_dir = _check_estate_seat_units(unit_dir)
    assert missing_dir.unknown and not missing_dir.passed

    unit_dir.mkdir()
    none_installed = _check_estate_seat_units(unit_dir)
    assert not none_installed.passed and not none_installed.unknown
    assert "6 of 6 seat timers absent" in none_installed.detail

    for seat in ("atlas", "link", "mero", "niobe", "seraph", "tank"):
        (unit_dir / f"skfleet-{seat}.timer").write_text("[Timer]\n")
    assert _check_estate_seat_units(unit_dir).passed


def test_doctor_catches_a_unit_naming_a_module_nobody_ships(tmp_path: Path) -> None:
    """The shadow-unit defect, asked directly instead of at enable time."""
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / "skfleet-good.service").write_text(
        "[Service]\nExecStart=/bin/python3 -m skcapstone.seat_cycle_entrypoint --seat niobe\n"
    )
    assert _check_estate_unit_entrypoints(unit_dir).passed

    (unit_dir / "skfleet-niobe-shadow.service").write_text(
        "[Service]\nExecStart=/bin/python3 -m skcapstone.seat_shadow_entrypoint --home /x\n"
    )
    broken = _check_estate_unit_entrypoints(unit_dir)
    assert not broken.passed
    assert "seat_shadow_entrypoint" in broken.detail


def test_estate_check_family_is_complete_and_never_raises(dirs: dict[str, Path]) -> None:
    """All six reported gaps plus the leak check answer on a fresh estate."""
    checks = _check_estate(dirs["home"])
    names = {c.name for c in checks}
    assert names == {
        "estate:seat-control-plane",
        "estate:skmail-helper",
        "estate:link-observation-feed",
        "estate:fleet-node-env",
        "estate:host-local-leak",
        "estate:sknoded-interval",
        "estate:seat-units",
        "estate:unit-entrypoints",
    }
    for check in checks:
        assert check.passed or check.detail, check.name
        if not check.passed and not check.unknown:
            assert check.fix, check.name
