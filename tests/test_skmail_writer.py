"""Canonical SKMail writer regression tests."""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
from pathlib import Path

PATH = Path(__file__).parents[1] / "scripts" / "fleet" / "skmail_writer.py"
SPEC = importlib.util.spec_from_file_location("skmail_writer", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_rejects_unexpanded_identity(tmp_path: Path) -> None:
    for sender in ("SKAGENT", "$SKAGENT", "${SKAGENT}"):
        try:
            MODULE.append(tmp_path, sender, "jarvis", "normal", "x", "y", "chiap01")
        except ValueError:
            continue
        raise AssertionError(sender)


def test_concurrent_append_is_complete_and_canonical(tmp_path: Path) -> None:
    def write(number: int) -> str:
        return MODULE.append(
            tmp_path, "worker", "jarvis", "normal", str(number), "body", "chiap01"
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        hashes = list(pool.map(write, range(40)))
    lines = (tmp_path / "worker@chiap01.jsonl").read_text().splitlines()
    assert len(lines) == len(set(hashes)) == 40
    assert all(json.loads(line)["from"] == "worker" for line in lines)


def test_partial_or_foreign_mailbox_fails_closed(tmp_path: Path) -> None:
    box = tmp_path / "worker@chiap01.jsonl"
    box.write_text('{"partial":true}')
    try:
        MODULE.append(tmp_path, "worker", "jarvis", "normal", "x", "y", "chiap01")
    except ValueError as exc:
        assert "partial" in str(exc)
    else:
        raise AssertionError("partial mailbox accepted")
