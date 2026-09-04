"""Hourly deduplication for repeated fleet blocker diagnostics."""

import ast
import datetime
import json
import os
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


class OsProxy:
    def __getattr__(self, name: str):
        return getattr(os, name)


def _helper(os_module=None):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_log_once_per_hour"
    )
    emitted = []
    namespace = {
        "datetime": datetime,
        "json": json,
        "os": os_module or os,
        "re": __import__("re"),
        "HOME": "/unused",
        "log": lambda directory, message: emitted.append((directory, message)),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["_log_once_per_hour"], emitted


def test_repeated_card_blocker_is_emitted_once_per_utc_hour(tmp_path: Path) -> None:
    emit_once, emitted = _helper()
    first_hour = datetime.datetime(2026, 9, 3, 18, 5, tzinfo=datetime.timezone.utc)
    next_hour = first_hour + datetime.timedelta(hours=1)

    assert emit_once(
        tmp_path, "OPEN_REVIEW_EVIDENCE_BLOCKED", "abc12345", "blocked", tmp_path, first_hour
    )
    assert not emit_once(
        tmp_path, "OPEN_REVIEW_EVIDENCE_BLOCKED", "abc12345", "blocked", tmp_path, first_hour
    )
    assert emit_once(
        tmp_path, "OPEN_REVIEW_EVIDENCE_BLOCKED", "abc12345", "blocked", tmp_path, next_hour
    )
    assert [message for _, message in emitted] == ["blocked", "blocked"]

    markers = sorted(tmp_path.glob("*.json"))
    assert len(markers) == 2
    assert json.loads(markers[0].read_text(encoding="utf-8")) == {
        "card_id": "abc12345",
        "event": "OPEN_REVIEW_EVIDENCE_BLOCKED",
        "hour_utc": "20260903T18",
    }


def test_each_card_keeps_its_first_occurrence(tmp_path: Path) -> None:
    emit_once, emitted = _helper()
    now = datetime.datetime(2026, 9, 3, 18, 59, tzinfo=datetime.timezone.utc)

    assert emit_once(tmp_path, "OPEN_REVIEW_EVIDENCE_BLOCKED", "card-one", "one", tmp_path, now)
    assert emit_once(tmp_path, "OPEN_REVIEW_EVIDENCE_BLOCKED", "card-two", "two", tmp_path, now)
    assert [message for _, message in emitted] == ["one", "two"]


def test_each_reaper_exclusion_reason_keeps_one_occurrence(tmp_path: Path) -> None:
    emit_once, emitted = _helper()
    now = datetime.datetime(2026, 9, 3, 18, 59, tzinfo=datetime.timezone.utc)

    assert emit_once(tmp_path, "REAP_EXCLUDED_REVISION", "card-one", "revision", tmp_path, now)
    assert not emit_once(tmp_path, "REAP_EXCLUDED_REVISION", "card-one", "revision", tmp_path, now)
    assert emit_once(tmp_path, "REAP_EXCLUDED_TIMESTAMP", "card-one", "timestamp", tmp_path, now)
    assert [message for _, message in emitted] == ["revision", "timestamp"]


def test_marker_write_failure_is_fail_open_and_removes_marker(tmp_path: Path) -> None:
    mocked_os = OsProxy()

    def fail_write(_fd: int, _payload: bytes) -> int:
        raise OSError("write failed")

    mocked_os.write = fail_write
    emit_once, emitted = _helper(mocked_os)
    now = datetime.datetime(2026, 9, 3, 18, 5, tzinfo=datetime.timezone.utc)

    assert emit_once(tmp_path, "BLOCKED", "card-one", "message", tmp_path, now)
    assert list(tmp_path.glob("*.json")) == []
    assert emitted == [(tmp_path, "message")]


def test_marker_fsync_failure_is_fail_open_and_removes_marker(tmp_path: Path) -> None:
    mocked_os = OsProxy()

    def fail_fsync(_fd: int) -> None:
        raise OSError("fsync failed")

    mocked_os.fsync = fail_fsync
    emit_once, emitted = _helper(mocked_os)
    now = datetime.datetime(2026, 9, 3, 18, 5, tzinfo=datetime.timezone.utc)

    assert emit_once(tmp_path, "BLOCKED", "card-one", "message", tmp_path, now)
    assert list(tmp_path.glob("*.json")) == []
    assert emitted == [(tmp_path, "message")]


def test_marker_close_failure_is_fail_open_and_removes_marker(tmp_path: Path) -> None:
    mocked_os = OsProxy()

    def close_then_fail(fd: int) -> None:
        os.close(fd)
        raise OSError("close failed")

    mocked_os.close = close_then_fail
    emit_once, emitted = _helper(mocked_os)
    now = datetime.datetime(2026, 9, 3, 18, 5, tzinfo=datetime.timezone.utc)

    assert emit_once(tmp_path, "BLOCKED", "card-one", "message", tmp_path, now)
    assert list(tmp_path.glob("*.json")) == []
    assert emitted == [(tmp_path, "message")]
