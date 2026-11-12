"""`fleet` is a broadcast address: every reader sees it, like `all`.

Before this fix `skmail read` matched only the reader's own name and `all`, so
mail addressed to `fleet` was returned to nobody, ever. 116 such messages (75
urgent) were stranded on chiap08 when this was found. The alias applies only to
mail sent after the rollout moment, so those old messages do not flood every
reader at once.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skmail"


def _rec(ts: str, to: str, re: str) -> dict[str, object]:
    return {
        "ts": ts,
        "from": "seraph",
        "to": to,
        "priority": "urgent",
        "re": re,
        "body": "b",
        "host": "chiap01",
    }


def _read(coord: Path, me: str, records: list[dict[str, object]]) -> str:
    box = coord / "skmail.d" / "seraph@chiap01.jsonl"
    box.parent.mkdir(parents=True, exist_ok=True)
    box.write_text("".join(json.dumps(r) + "\n" for r in records))
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(coord)
    return subprocess.run(
        [str(SCRIPT), "read", me], check=True, capture_output=True, env=env, text=True
    ).stdout


def test_new_fleet_broadcast_reaches_every_reader(tmp_path: Path) -> None:
    out = _read(tmp_path, "worker7", [_rec("2026-10-04T00:00:00+00:00", "fleet", "NEWFLEET")])
    assert "NEWFLEET" in out


def test_fleet_alias_is_case_insensitive(tmp_path: Path) -> None:
    out = _read(tmp_path, "worker7", [_rec("2026-10-04T00:00:00+00:00", "FLEET", "UPPERFLEET")])
    assert "UPPERFLEET" in out


def test_pre_rollout_fleet_mail_stays_dark(tmp_path: Path) -> None:
    out = _read(tmp_path, "worker7", [_rec("2026-09-01T00:00:00+00:00", "fleet", "OLDFLEET")])
    assert "OLDFLEET" not in out


def test_all_and_own_name_unchanged(tmp_path: Path) -> None:
    out = _read(
        tmp_path,
        "worker7",
        [
            _rec("2026-09-01T00:00:00+00:00", "all", "OLDALL"),
            _rec("2026-09-01T00:00:00+00:00", "worker7", "MINE"),
            _rec("2026-09-01T00:00:00+00:00", "someoneelse", "NOTMINE"),
        ],
    )
    assert "OLDALL" in out and "MINE" in out
    assert "NOTMINE" not in out


def test_ack_covers_fleet_broadcast(tmp_path: Path) -> None:
    rec = [_rec("2026-10-04T00:00:00+00:00", "fleet", "ACKME")]
    _read(tmp_path, "worker7", rec)
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(tmp_path)
    subprocess.run(
        [str(SCRIPT), "ack", "worker7"], check=True, capture_output=True, env=env, text=True
    )
    assert "ACKME" not in _read(tmp_path, "worker7", rec)
