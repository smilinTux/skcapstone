"""`lumina` and `lumina-nor` are one agent, so one reader drains both names.

Chef's Lumina answers to two addresses: `lumina` was the original chi name and
`lumina-nor` the noroc2027 one. The reader matched only the reader's exact name,
so every reader ran as `lumina-nor` while mail addressed to `lumina` piled up
untouched: 5,876 unread on chiap08 when this was found, and Jarvis had started
dual-sending to both names to work around it.

Same defect shape as the `fleet` broadcast alias, so the same containment: the
alias applies only to mail sent after the rollout moment, so the old backlog
does not flood the live channel. Each name keeps its own read cursor; the alias
widens which mail is eligible, it does not merge two cursors into one.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skmail"

AFTER = "2026-10-07T00:00:00+00:00"
BEFORE = "2026-10-01T00:00:00+00:00"


def _rec(ts: str, to: str, re: str) -> dict[str, object]:
    return {
        "ts": ts,
        "from": "jarvis",
        "to": to,
        "priority": "urgent",
        "re": re,
        "body": "b",
        "host": "chiap01",
    }


def _read(coord: Path, me: str, records: list[dict[str, object]]) -> str:
    box = coord / "skmail.d" / "jarvis@chiap01.jsonl"
    box.parent.mkdir(parents=True, exist_ok=True)
    box.write_text("".join(json.dumps(r) + "\n" for r in records))
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(coord)
    return subprocess.run(
        [str(SCRIPT), "read", me], check=True, capture_output=True, env=env, text=True
    ).stdout


def test_lumina_mail_reaches_lumina_nor(tmp_path: Path) -> None:
    out = _read(tmp_path, "lumina-nor", [_rec(AFTER, "lumina", "TOPLAIN")])
    assert "TOPLAIN" in out


def test_lumina_nor_mail_reaches_lumina(tmp_path: Path) -> None:
    out = _read(tmp_path, "lumina", [_rec(AFTER, "lumina-nor", "TONOR")])
    assert "TONOR" in out


def test_alias_is_case_insensitive(tmp_path: Path) -> None:
    out = _read(tmp_path, "lumina-nor", [_rec(AFTER, "LUMINA", "UPPERLUMINA")])
    assert "UPPERLUMINA" in out


def test_pre_rollout_backlog_stays_dark(tmp_path: Path) -> None:
    """The 5,876-deep backlog must not flood the working lumina-nor channel."""
    out = _read(tmp_path, "lumina-nor", [_rec(BEFORE, "lumina", "OLDBACKLOG")])
    assert "OLDBACKLOG" not in out


def test_own_exact_name_unaffected_by_the_cutoff(tmp_path: Path) -> None:
    """Existing cursors keep working: an exact-name match is never gated."""
    out = _read(tmp_path, "lumina-nor", [_rec(BEFORE, "lumina-nor", "OLDMINE")])
    assert "OLDMINE" in out


def test_alias_does_not_leak_to_other_agents(tmp_path: Path) -> None:
    out = _read(
        tmp_path,
        "jarvis",
        [_rec(AFTER, "lumina", "NOTJARVIS"), _rec(AFTER, "lumina-nor", "ALSONOT")],
    )
    assert "NOTJARVIS" not in out and "ALSONOT" not in out


def test_lumina_does_not_receive_unrelated_mail(tmp_path: Path) -> None:
    out = _read(
        tmp_path,
        "lumina-nor",
        [_rec(AFTER, "jarvis", "JARVISONLY"), _rec(AFTER, "seraph", "SERAPHONLY")],
    )
    assert "JARVISONLY" not in out and "SERAPHONLY" not in out


def test_all_and_fleet_still_reach_lumina_nor(tmp_path: Path) -> None:
    out = _read(
        tmp_path,
        "lumina-nor",
        [_rec(BEFORE, "all", "BROADCAST"), _rec(AFTER, "fleet", "FLEETCAST")],
    )
    assert "BROADCAST" in out and "FLEETCAST" in out


def test_ack_covers_alias_delivered_mail(tmp_path: Path) -> None:
    rec = [_rec(AFTER, "lumina", "ACKALIAS")]
    _read(tmp_path, "lumina-nor", rec)
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(tmp_path)
    subprocess.run(
        [str(SCRIPT), "ack", "lumina-nor"], check=True, capture_output=True, env=env, text=True
    )
    assert "ACKALIAS" not in _read(tmp_path, "lumina-nor", rec)


def test_each_name_keeps_its_own_cursor(tmp_path: Path) -> None:
    """Acking as lumina-nor must not silently mark lumina's backlog read."""
    rec = [_rec(AFTER, "lumina", "SHARED")]
    _read(tmp_path, "lumina-nor", rec)
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(tmp_path)
    subprocess.run(
        [str(SCRIPT), "ack", "lumina-nor"], check=True, capture_output=True, env=env, text=True
    )
    assert "SHARED" not in _read(tmp_path, "lumina-nor", rec)
    assert "SHARED" in _read(tmp_path, "lumina", rec)
