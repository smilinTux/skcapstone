"""Regression tests for the append-only skmail reader."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skmail"


def _record(**overrides: object) -> dict[str, object]:
    """Return one valid canonical mail record with selected overrides."""
    record: dict[str, object] = {
        "ts": "2026-09-01T00:00:00+00:00",
        "from": "alice",
        "to": "jarvis",
        "priority": "normal",
        "re": "test",
        "body": "body",
        "host": "chiap01",
    }
    record.update(overrides)
    return record


def _mailbox(coord: Path, name: str, records: list[dict[str, object]]) -> Path:
    """Write an isolated append-only mailbox fixture."""
    box = coord / "skmail.d" / name
    box.parent.mkdir(parents=True)
    box.write_text("".join(json.dumps(record) + "\n" for record in records))
    return box


def _tail(coord: Path) -> subprocess.CompletedProcess[str]:
    """Run the reader against an isolated mailbox directory."""
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(coord)
    return subprocess.run(
        [str(SCRIPT), "tail", "100"],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )


def test_bare_recipient_layout_is_accepted(tmp_path: Path) -> None:
    """A bare recipient mailbox does not assert a writer identity."""
    _mailbox(tmp_path, "jarvis.jsonl", [_record(**{"from": "worker"})])

    result = _tail(tmp_path)

    assert "worker -> jarvis" in result.stdout
    assert "writer_file_mismatch" not in result.stderr


def test_explicit_legacy_recipient_layout_is_accepted(tmp_path: Path) -> None:
    """A closed legacy recipient mailbox accepts its historical writer."""
    _mailbox(
        tmp_path,
        "lumina@chiap03.jsonl",
        [_record(**{"from": "pi-glm-worker", "to": "lumina", "host": "chiap03"})],
    )

    result = _tail(tmp_path)

    assert "pi-glm-worker -> lumina" in result.stdout
    assert "writer_file_mismatch" not in result.stderr


def test_mixed_legacy_recipient_layout_is_accepted(tmp_path: Path) -> None:
    """Self-sent and received records can coexist in a known legacy box."""
    _mailbox(
        tmp_path,
        "jarvis@chiap03.jsonl",
        [
            _record(**{"from": "jarvis", "host": "chiap03"}),
            _record(**{"from": "pi-glm-worker", "host": "chiap03"}),
        ],
    )

    result = _tail(tmp_path)

    assert "jarvis -> jarvis" in result.stdout
    assert "pi-glm-worker -> jarvis" in result.stdout
    assert "writer_file_mismatch" not in result.stderr


def test_forged_writer_only_file_is_rejected(tmp_path: Path) -> None:
    """Payload fields cannot reclassify an unlisted writer box."""
    _mailbox(tmp_path, "alice@chiap01.jsonl", [_record(**{"from": "mallory"})])

    result = _tail(tmp_path)

    assert result.stdout == ""
    assert "reason=writer_file_mismatch" in result.stderr


def test_forged_line_in_mixed_writer_file_is_rejected(tmp_path: Path) -> None:
    """A valid sibling does not hide forgery in a writer box."""
    _mailbox(
        tmp_path,
        "alice@chiap01.jsonl",
        [_record(), _record(**{"from": "mallory"})],
    )

    result = _tail(tmp_path)

    assert "alice -> jarvis" in result.stdout
    assert "mallory -> jarvis" not in result.stdout
    assert result.stderr.count("reason=writer_file_mismatch") == 1


def test_aliases_and_default_priority_are_accepted_without_rewrite(
    tmp_path: Path,
) -> None:
    """Aliases are normalized in memory and source bytes remain unchanged."""
    box = _mailbox(
        tmp_path,
        "jarvis.jsonl",
        [
            {
                "timestamp": "2026-09-01T00:00:00+00:00",
                "sender": "worker",
                "recipient": "jarvis",
                "subject": "aliases",
                "body": "body",
            }
        ],
    )
    before = box.read_bytes()

    result = _tail(tmp_path)

    assert "[normal] worker -> jarvis  re aliases" in result.stdout
    assert result.stderr == ""
    assert box.read_bytes() == before


def test_skmail_is_valid_bash(skmail_installation) -> None:
    """The embedded Python remains safely quoted by the Bash wrapper."""
    subprocess.run(["bash", "-n", str(skmail_installation["package"] / "skmail")], check=True)
