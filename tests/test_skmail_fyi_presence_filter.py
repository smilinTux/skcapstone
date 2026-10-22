"""`read` hides `fyi` and legacy SEAT-HELLO so real mail is reachable.

`skmail` has had three priorities (urgent | normal | fyi) all along, but `read`
showed every one of them. Presence telemetry therefore drowned real mail: every
seat start broadcasts `SEAT-HELLO-<seat>` to `all` in an append-only log, so one
inbox reached 506 unread of which 496 were hellos (206 from a single seat), and
the three messages that actually mattered sat behind them, past the ~8 KiB read
cap, effectively unreachable. Nothing reads a SEAT-HELLO; it is write-only.

Two halves, because the log is append-only:
  * the sender now classifies them `fyi` (seat_mail), which `read` hides;
  * the 496 already written as `normal` can never be re-classified, so the
    default view also suppresses them by subject. That clause ages out.

`(N new)` keeps its exact shape, since seat_mail._NEW_COUNT is anchored at line
end on it; a different final line would make every seat silently report zero.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "fleet" / "skmail"


def _rec(ts: str, re_: str, priority: str = "normal", to: str = "all") -> dict[str, object]:
    return {
        "ts": ts,
        "from": "seraph",
        "to": to,
        "priority": priority,
        "re": re_,
        "body": "body text",
        "host": "chiap01",
    }


def _read(coord: Path, me: str, records: list[dict[str, object]], *args: str) -> str:
    box = coord / "skmail.d" / "seraph@chiap01.jsonl"
    box.parent.mkdir(parents=True, exist_ok=True)
    box.write_text("".join(json.dumps(r) + "\n" for r in records))
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(coord)
    # No PYTHONPATH juggling here on purpose: the autouse `_installed_skmail_tests`
    # fixture (tests/skmail_installation.py, fires for any test_skmail_* file)
    # installs this tree into an isolated venv, clears PYTHONPATH and repoints
    # SCRIPT at the installed binary. So these run against a real install, not
    # whatever editable checkout happens to be on sys.path.
    return subprocess.run(
        [str(SCRIPT), "read", me, *args], check=True, capture_output=True, env=env, text=True
    ).stdout


def _new_count(output: str) -> int:
    """Parse exactly as seat_mail._NEW_COUNT does."""
    match = re.search(r"\((\d+) new\)\s*$", output, re.MULTILINE)
    assert match, f"no anchored (N new) line in: {output[-200:]!r}"
    return int(match.group(1))


REAL = "2026-10-22T03:51:00+00:00"
HELLO = "2026-10-22T04:00:00+00:00"
FYI = "2026-10-22T04:30:00+00:00"


def test_legacy_seat_hello_is_hidden_and_real_mail_surfaces(tmp_path: Path) -> None:
    """The reported failure: real mail buried behind normal-priority hellos."""
    records = [_rec(REAL, "Actual question for you")]
    records += [_rec(f"2026-10-22T04:{m:02d}:00+00:00", "SEAT-HELLO-link") for m in range(1, 40)]
    out = _read(tmp_path, "lumina", records)
    assert "Actual question for you" in out
    assert "SEAT-HELLO-link" not in out
    assert _new_count(out) == 1, "the hellos must not be counted as actionable"


def test_fyi_priority_is_hidden_by_default(tmp_path: Path) -> None:
    out = _read(tmp_path, "lumina", [_rec(REAL, "real one"), _rec(FYI, "noise", "fyi")])
    assert "real one" in out
    assert "noise" not in out
    assert _new_count(out) == 1


def test_fyi_view_shows_them_and_does_not_arm_ack(tmp_path: Path) -> None:
    out = _read(tmp_path, "lumina", [_rec(REAL, "real one"), _rec(FYI, "noise", "fyi")], "--fyi")
    assert "noise" in out
    assert "real one" not in out
    assert "does not arm ack" in out


def test_include_fyi_restores_the_old_behaviour(tmp_path: Path) -> None:
    out = _read(
        tmp_path, "lumina", [_rec(REAL, "real one"), _rec(FYI, "noise", "fyi")], "--include-fyi"
    )
    assert "real one" in out and "noise" in out
    assert _new_count(out) == 2


def test_urgent_still_wins_over_the_fyi_filter(tmp_path: Path) -> None:
    """An urgent message must never be filtered, whatever else is going on."""
    records = [_rec(REAL, "stop everything", "urgent"), _rec(HELLO, "SEAT-HELLO-link")]
    out = _read(tmp_path, "lumina", records)
    assert "stop everything" in out


def test_new_count_line_keeps_the_shape_seat_mail_parses(tmp_path: Path) -> None:
    """Guard the landmine: a different final line zeroes every seat's mail poll."""
    out = _read(tmp_path, "seraph", [_rec(REAL, "SEAT-HELLO-link")])
    assert out.rstrip().endswith("(0 new)")
    assert _new_count(out) == 0


def test_seat_hello_is_sent_as_fyi_not_normal() -> None:
    """The sender half: future hellos must arrive already classified.

    Reads startup_hello() rather than running it, because sending for real
    would append to the live append-only log.
    """
    source = (ROOT / "src" / "skcapstone" / "seat_mail.py").read_text(encoding="utf-8")
    body = source[source.index("def startup_hello") : source.index("def poll_mail")]
    assert '"fyi"' in body, "SEAT-HELLO must be sent at fyi priority"
    assert '"normal"' not in body, "SEAT-HELLO must no longer be sent at normal priority"
