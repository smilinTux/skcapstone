"""Tests for the fenced Link and Mero recurring entrypoint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import skcapstone.seat_cycle_entrypoint as seat_entrypoint
from skcapstone.seat_cycle_entrypoint import link_operation, load_control_plane, run_cycle


@pytest.fixture(autouse=True)
def user_home(tmp_path, monkeypatch):
    """A USER home that is NOT the estate home these operations are passed.

    This separation is the whole point. On every live host the two are
    different directories -- the estate home is a subdirectory of the user
    home -- and ``.local/bin`` hangs off the USER home. This module used to
    pass ``tmp_path`` as the estate home AND deploy the dispatcher to
    ``tmp_path/.local/bin``, collapsing the two into one directory. Under
    that collapse both the correct and the incorrect base resolved to the
    same file, so 30 tests here passed while production had been resolving
    the dispatcher to a path under the estate home that has never existed,
    reporting ``seraph_dispatcher_missing`` and suppressing every
    independent-review batch for 19 hours.

    Keeping them distinct is what gives these tests the ability to fail if
    the estate home is ever passed as the artifact base again: that base
    resolves under ``tmp_path``, where no dispatcher is deployed.

    ``HOME`` is set rather than ``Path.home`` patched so the child
    processes these operations spawn see the same home the parent resolved.
    """

    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def control(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {seat: ["chiap08"] for seat in ("link", "mero")},
            }
        ),
        encoding="utf-8",
    )


def test_inactive_host_refuses_before_operation(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    called = []
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap01",
        operation=lambda: called.append(True) or {"recommendations": 1},
    )
    assert result.result == "inactive_host_refused"
    assert called == []
    health = (tmp_path / "home/coordination/seat-cycles/link.health.jsonl").read_text()
    assert "inactive_host_refused" in health


def test_inactive_host_entrypoint_exits_successfully(tmp_path: Path, monkeypatch, capsys) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    monkeypatch.setattr(seat_entrypoint.socket, "gethostname", lambda: "chiap03")

    result = seat_entrypoint.main(
        ["--seat", "mero", "--home", str(tmp_path / "home"), "--control-plane", str(control_path)]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["result"] == "inactive_host_refused"


def test_dry_run_is_fenced_and_bounded(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    called = []
    result = run_cycle(
        seat="mero",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
        dry_run=True,
        operation=lambda: called.append(True) or {"recommendations": 9},
    )
    assert result.result == "dry_run"
    assert result.recommendations == 0
    assert called == []


def test_active_cycle_records_operation_summary(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
        operation=lambda: {"cards_examined": 4, "recommendations": 2, "suppressed": 1},
    )
    assert result.result == "complete"
    assert (tmp_path / "home/coordination/seat-cycles/link.cycle.receipts.jsonl").exists()
    assert result.cards_examined == 4
    assert result.recommendations == 2


def test_stale_feed_emits_only_signed_lineage_review_work(tmp_path: Path) -> None:
    home = tmp_path / "home"
    lineage = home / "coordination/link-lineage.json"
    lineage.parent.mkdir(parents=True)
    item = {
        "kind": "review-work",
        "reason": "missing_terminal_review",
        "repository": "org/repo",
        "pr": 7,
        "head_revision": "a" * 40,
        "base_revision": "b" * 40,
        "source_card": "source01",
        "card_generation": "2" * 64,
        "source_owner": "builder",
        "reviewer_candidates": [
            {"name": "Seraph", "seat": "seraph", "identity": "seraph", "eligible": True}
        ],
    }
    manifest = {
        "schema": "skfleet.link-lineage/v1",
        "source_revision": "1" * 64,
        "coverage": {"unresolved": 1},
        "records": {},
        "reviewer_candidates": [],
        "diagnostics": [],
        "unresolved_prs": [7],
        "review_work_recommendations": [item],
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest["evidence_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    lineage.write_text(json.dumps(manifest))
    result = link_operation(home, home / "coordination/missing-feed.json", lineage)
    assert result["reason"].startswith("review_work_only:")
    assert result["recommendations"] == 1
    emitted = json.loads(
        (home / "coordination/seat-cycles/link.review-work.jsonl").read_text().strip()
    )
    assert emitted["head_revision"] == "a" * 40
    assert emitted["source_card"] == "source01"
    assert emitted["reconciliation"]["launchable"] is False
    assert emitted["reconciliation"]["reason"] == "source_card_missing"


def test_cycle_without_operation_is_explicit_noop(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
    )
    assert result.result == "operation_not_configured"
    assert result.reason == "operation_not_configured"


def test_control_plane_requires_all_six_seats(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {"link": ["chiap08"]},
            }
        ),
        encoding="utf-8",
    )
    try:
        load_control_plane(path)
    except ValueError as exc:
        assert "not provisioned on active host" in str(exc)
    else:
        raise AssertionError("incomplete control plane accepted")


def test_unit_templates_preserve_limits_and_disabled_install_contract() -> None:
    root = Path(__file__).parents[1]
    link = (root / "systemd/skfleet-link.service").read_text()
    producer = (root / "systemd/skfleet-link-producer.service").read_text()
    producer_timer = (root / "systemd/skfleet-link-producer.timer").read_text()
    mero = (root / "systemd/skfleet-mero.service").read_text()
    link_timer = (root / "systemd/skfleet-link.timer").read_text()
    mero_timer = (root / "systemd/skfleet-mero.timer").read_text()
    assert "TimeoutStartSec=120" in link
    assert "SKAGENT=link-producer" in producer
    assert (
        "SKFLEET_LINK_REPOSITORIES=smilinTux/skcapstone,smilinTux/skdashboard,"
        "smilinTux/skworld,smilinTux/sk-standards" in producer
    )
    assert "Before=skfleet-link.service" in producer
    assert "skfleet-link-producer.py" in producer
    assert "OnBootSec=1min" in producer_timer
    assert "OnUnitActiveSec=5min" in producer_timer
    assert "TimeoutStartSec=180" in mero
    assert "--seat link" in link and "--seat mero" in mero
    assert "OnUnitActiveSec=5min" in link_timer
    assert "OnUnitActiveSec=5min" in mero_timer
    assert "skfleet-mero.service" in mero_timer
    for retired in ("seat-cycle", "atlas", "seraph", "niobe", "niobe-live"):
        for suffix in ("service", "timer"):
            assert not (root / "systemd" / f"skfleet-{retired}.{suffix}").exists()
            assert not (
                root / "src" / "skcapstone" / "data" / "systemd" / f"skfleet-{retired}.{suffix}"
            ).exists()
    assert not (root / "systemd/skfleet-tank.service").exists()
    assert not (root / "systemd/skfleet-tank.timer").exists()
    assert not (root / "src/skcapstone/data/systemd/skfleet-tank.service").exists()
    assert not (root / "src/skcapstone/data/systemd/skfleet-tank.timer").exists()


def _claim(config_home: Path, host: str) -> None:
    """Write this machine's host-local lifecycle claim."""
    path = config_home / "skcapstone/lifecycle-host.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": "sk.lifecycle-host/v1", "active_host": host}), encoding="utf-8"
    )


def test_a_second_estate_may_elect_its_own_active_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same six seats run on noroc2027, with that host's own claim agreeing."""
    control_path = tmp_path / "control.json"
    control_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "nor-r1",
                "active_host": "noroc2027",
                "seats": {seat: ["noroc2027"] for seat in ("link", "mero")},
            }
        ),
        encoding="utf-8",
    )
    config_home = tmp_path / "config"
    _claim(config_home, "noroc2027")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    assert load_control_plane(control_path, host="noroc2027")["active_host"] == "noroc2027"


def test_synced_record_cannot_activate_a_host_that_never_claimed_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chi record arriving on a nor machine is refused by that machine's claim."""
    control_path = tmp_path / "control.json"
    control(control_path)
    config_home = tmp_path / "config"
    _claim(config_home, "noroc2027")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    with pytest.raises(ValueError, match="this machine claims noroc2027"):
        load_control_plane(control_path, host="noroc2027")


def test_a_claim_copied_to_another_node_refuses_rather_than_promoting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    config_home = tmp_path / "config"
    _claim(config_home, "noroc2027")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    with pytest.raises(ValueError, match="this machine is chiap08"):
        load_control_plane(control_path, host="chiap08")


def test_a_machine_with_no_claim_still_follows_the_estate_election(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim only ever refuses: absent, the synced election stands."""
    control_path = tmp_path / "control.json"
    control(control_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    assert load_control_plane(control_path, host="chiap08")["active_host"] == "chiap08"


@pytest.mark.parametrize("seat", ["atlas", "seraph", "niobe", "tank"])
def test_retired_dispatcher_seats_are_refused(tmp_path: Path, seat: str) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    with pytest.raises(ValueError, match="unsupported recurring seat"):
        run_cycle(
            seat=seat,
            home=tmp_path / "home",
            control_plane=control_path,
            local_host="chiap08",
        )
    with pytest.raises(SystemExit):
        seat_entrypoint.main(
            ["--seat", seat, "--home", str(tmp_path), "--control-plane", str(control_path)]
        )
