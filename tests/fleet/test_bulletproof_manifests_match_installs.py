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
