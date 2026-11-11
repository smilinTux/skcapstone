"""The bulletproof fleet declarations must name what is actually installed.

CMDB drift matches a fleet object to a running thing by name: ``spec.unit``
for a unit-backed job, the ``sk-cron-run`` job name for a crontab job. The
CR-7.3 manifests were authored from runbooks and never cross-checked, so on
2026-11-11 a third of the "missing" findings on noroc2027 were jobs that were
running under another name (SKBrain CmdbDriftBounded triage).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "deploy" / "fleet-objects" / "bulletproof"


def _spec(kind: str, name: str) -> dict:
    return json.loads((ROOT / kind / f"{name}.json").read_text(encoding="utf-8"))["spec"]


def test_telegram_bridge_names_the_unit_skchat_ships() -> None:
    # skchat systemd/units/skchat-telegram-lumina.service is the shipped unit.
    assert _spec("service", "skchat-telegram-bridge-lumina")["unit"] == (
        "skchat-telegram-lumina.service"
    )


def test_skchat_backup_names_its_timer_and_real_script() -> None:
    spec = _spec("cronjob", "skchat-backup-offbox")
    assert spec["unit"] == "skchat-backup.timer"
    assert spec["command"] == "scripts/backup-skchat.sh"


def test_gfs_backup_names_the_script_in_this_repo() -> None:
    command = _spec("cronjob", "skcapstone-backup-gfs")["command"]
    assert (Path(__file__).resolve().parents[2] / command).is_file()


def test_skmem_reconcile_names_a_command_that_exists() -> None:
    # `skmemory reconcile` is not a subcommand; the module entry point is.
    assert _spec("cronjob", "skmem-reconcile")["command"] == "python3 -m skmemory.reconcile --all"


def test_no_manifest_hardcodes_a_home_directory() -> None:
    for path in ROOT.rglob("*.json"):
        assert "/home/" not in path.read_text(encoding="utf-8"), path.name


def test_every_manifest_still_normalises() -> None:
    from skcapstone.fleet.cron import normalize_cronjob_spec
    from skcapstone.fleet.services import normalize_service_spec

    for path in (ROOT / "cronjob").glob("*.json"):
        normalize_cronjob_spec(json.loads(path.read_text())["spec"])
    for path in (ROOT / "service").glob("*.json"):
        normalize_service_spec(json.loads(path.read_text())["spec"])


RETIRED = {
    "cronjob": {"skmem-gfs-tarball", "skmem-health", "skmem-reindex", "skos-morning-brief",
                "skgateway-parity-check"},
    "service": {"skingest", "skmemory-daemon", "skos-scheduler"},
}


def test_retired_declarations_are_tombstoned_with_a_reason() -> None:
    """Retired, not deleted: the file stays as the generation ledger, and
    ``spec.deleted`` makes the controllers and discovery ignore it."""
    for kind, names in RETIRED.items():
        for name in names:
            spec = _spec(kind, name)
            assert spec["deleted"] is True, name
            assert spec["retired"].startswith("Retired 2026-11-11"), name


def test_no_live_declaration_is_tombstoned_by_accident() -> None:
    for kind in ("cronjob", "service"):
        for path in (ROOT / kind).glob("*.json"):
            if path.stem not in RETIRED[kind]:
                assert not _spec(kind, path.stem).get("deleted"), path.stem


def test_capauth_keystore_is_the_capauth_authz_unit() -> None:
    spec = _spec("service", "capauth-keystore")
    assert (spec["runtime"], spec["unit"]) == ("systemd-user", "capauth-authz.service")
    assert "compose" not in spec


def test_housekeep_names_the_timer_skcomms_ships() -> None:
    assert _spec("cronjob", "skcomms-housekeep")["unit"] == "skcomms-housekeep.timer"
