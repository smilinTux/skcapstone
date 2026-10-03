"""`skmail read` is capped, and `ack` never moves past mail the reader was not shown.

A reader with a large backlog used to get every unread message at once. On
chiap08 on 2026-10-03 that was 10,791 messages (7.0 MB) for jarvis, more than any
context window can hold, so the mailbox could not be read at all. Worse, `ack`
moved the cursor to the newest message of EVERYTHING addressed to the reader, so
capping `read` alone would have made `read; ack` silently discard the unshown
remainder. These tests pin both halves.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skmail"


def _box(coord: Path, n: int, prio: str = "normal", to: str = "worker7") -> None:
    box = coord / "skmail.d" / "seraph@chiap01.jsonl"
    box.parent.mkdir(parents=True, exist_ok=True)
    with box.open("a") as f:
        for i in range(n):
            f.write(
                json.dumps(
                    {
                        "ts": "2026-09-%02dT%02d:%02d:00+00:00"
                        % (1 + i // 1440, (i // 60) % 24, i % 60),
                        "from": "seraph",
                        "to": to,
                        "priority": prio if i % 3 == 0 else "normal",
                        "re": "subject-%d" % i,
                        "body": "body %d " % i + "x" * 200,
                        "host": "chiap01",
                    }
                )
                + "\n"
            )


def _run(coord: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(coord)
    return subprocess.run(
        [str(SCRIPT), *args], check=check, capture_output=True, env=env, text=True
    )


def _shown(out: str) -> list[str]:
    return [line.split(" re ", 1)[1] for line in out.splitlines() if " re subject-" in line]


def test_read_is_capped_and_says_how_many_remain(tmp_path: Path) -> None:
    _box(tmp_path, 200)
    out = _run(tmp_path, "read", "worker7").stdout
    shown = _shown(out)
    assert 0 < len(shown) < 200
    assert len(out.encode()) < 16384
    assert "more unread" in out
    assert shown[0] == "subject-0"


def test_read_then_ack_never_skips_unshown_mail(tmp_path: Path) -> None:
    _box(tmp_path, 200)
    seen: list[str] = []
    for _ in range(100):
        out = _run(tmp_path, "read", "worker7").stdout
        batch = _shown(out)
        if not batch:
            break
        seen += batch
        _run(tmp_path, "ack", "worker7")
    assert seen == ["subject-%d" % i for i in range(200)]


def test_ack_without_a_read_refuses_and_moves_nothing(tmp_path: Path) -> None:
    _box(tmp_path, 5)
    result = _run(tmp_path, "ack", "worker7", check=False)
    assert result.returncode != 0
    assert "read" in (result.stderr + result.stdout)
    assert len(_shown(_run(tmp_path, "read", "worker7").stdout)) == 5


def test_summary_and_urgent_views_do_not_arm_ack(tmp_path: Path) -> None:
    _box(tmp_path, 30, prio="urgent")
    summary = _run(tmp_path, "read", "worker7", "--summary").stdout
    assert "30 unread" in summary
    urgent = _shown(_run(tmp_path, "read", "worker7", "--urgent").stdout)
    assert urgent and all(int(s.split("-")[1]) % 3 == 0 for s in urgent)
    assert _run(tmp_path, "ack", "worker7", check=False).returncode != 0


def test_ack_before_skips_a_backlog_deliberately(tmp_path: Path) -> None:
    _box(tmp_path, 120)
    _run(tmp_path, "ack", "worker7", "--before", "2026-09-01T01:00:00+00:00")
    out = _run(tmp_path, "read", "worker7", "--all").stdout
    assert _shown(out)[0] == "subject-60"


def test_small_mailbox_reads_whole_and_acks_clean(tmp_path: Path) -> None:
    _box(tmp_path, 3)
    out = _run(tmp_path, "read", "worker7").stdout
    assert len(_shown(out)) == 3 and "more unread" not in out
    _run(tmp_path, "ack", "worker7")
    assert "(0 new)" in _run(tmp_path, "read", "worker7").stdout
