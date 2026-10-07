"""Incremental launch-history cache coverage for the fleet rotator."""

from __future__ import annotations

import ast
import collections
import datetime
import fcntl
import json
import os
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
FUNCTIONS = {
    "_scan_launch_history_log",
    "_aggregate_launch_history",
    "_scan_launch_history_full",
    "_load_launch_history",
}


def _namespace() -> dict[str, object]:
    """Extract the launch-history helpers without executing the rotator."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
    ]
    namespace = {
        "collections": collections,
        "datetime": datetime,
        "fcntl": fcntl,
        "json": json,
        "os": os,
        "Path": Path,
        "stat": stat,
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert FUNCTIONS <= namespace.keys()
    return namespace


def _launch(card: str, model: str = "glm-m") -> str:
    """Return one canonical launch row accepted by the rotation log reader."""
    return (
        f"LAUNCHED|chiap01|pi-glm-{card}|{card}|lane=glm|model={model}|"
        f"owner=pi-glm-{card}|claim_revision=rev-{card}\n"
    )


def _write(root: Path, stamp: str, *lines: str) -> Path:
    """Write one timestamped action log in the test evidence tree."""
    directory = root / stamp
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "actions.log"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def test_cold_build_aggregates_launch_timestamps_and_classifications(tmp_path: Path) -> None:
    ns = _namespace()
    root = tmp_path / "evidence"
    first = "20261001T000000Z"
    second = "20261002T000000Z"
    _write(root, first, _launch("a0000001"))
    _write(
        root,
        second,
        _launch("a0000001", "gpt-5.6-sol"),
        "LAUNCHED|chiap02|owner|a0000002|legacy\n",
    )

    launched, wake, strong = ns["_load_launch_history"](
        root, tmp_path / "cache" / "history.json", "gpt-5.6-sol"
    )

    epoch = (
        lambda stamp: datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
        .replace(tzinfo=datetime.timezone.utc)
        .timestamp()
    )
    assert launched == {"a0000001": epoch(second), "a0000002": epoch(second)}
    assert dict(wake) == {"a0000001": [epoch(first), epoch(second)]}
    assert strong == {"a0000001": epoch(second)}


def test_unchanged_logs_are_not_reparsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ns = _namespace()
    root = tmp_path / "evidence"
    _write(root, "20261001T000000Z", _launch("a0000001"))
    cache = tmp_path / "cache" / "history.json"
    expected = ns["_load_launch_history"](root, cache, "gpt-5.6-sol")
    ns["_scan_launch_history_log"] = lambda *_args: pytest.fail("unchanged file reparsed")

    actual = ns["_load_launch_history"](root, cache, "gpt-5.6-sol")

    assert actual == expected


def test_appended_launch_updates_only_changed_file(tmp_path: Path) -> None:
    ns = _namespace()
    root = tmp_path / "evidence"
    first = _write(root, "20261001T000000Z", _launch("a0000001"))
    second = _write(root, "20261002T000000Z", _launch("a0000002"))
    cache = tmp_path / "cache" / "history.json"
    ns["_load_launch_history"](root, cache, "gpt-5.6-sol")
    second.write_text(second.read_text(encoding="utf-8") + _launch("a0000002"), encoding="utf-8")
    parsed: list[str] = []
    scan = ns["_scan_launch_history_log"]
    ns["_scan_launch_history_log"] = lambda path, epoch, model: (
        parsed.append(str(path)) or scan(path, epoch, model)
    )

    launched, wake, _strong = ns["_load_launch_history"](root, cache, "gpt-5.6-sol")

    assert set(launched) == {"a0000001", "a0000002"}
    assert len(wake["a0000002"]) == 2
    assert parsed == [str(second)]
    assert first.exists()


def test_truncated_or_removed_log_replaces_its_cached_rows(tmp_path: Path) -> None:
    ns = _namespace()
    root = tmp_path / "evidence"
    log = _write(root, "20261001T000000Z", _launch("a0000001"), _launch("a0000002"))
    cache = tmp_path / "cache" / "history.json"
    ns["_load_launch_history"](root, cache, "gpt-5.6-sol")
    log.write_text(_launch("a0000003"), encoding="utf-8")

    launched, _wake, _strong = ns["_load_launch_history"](root, cache, "gpt-5.6-sol")
    assert set(launched) == {"a0000003"}

    replacement = root / "replacement.log"
    replacement.write_text(_launch("a0000004"), encoding="utf-8")
    replacement.replace(log)
    launched, _wake, _strong = ns["_load_launch_history"](root, cache, "gpt-5.6-sol")
    assert set(launched) == {"a0000004"}

    log.unlink()
    launched, _wake, _strong = ns["_load_launch_history"](root, cache, "gpt-5.6-sol")
    assert launched == {}


def test_malformed_cache_rebuilds_from_source_logs(tmp_path: Path) -> None:
    ns = _namespace()
    root = tmp_path / "evidence"
    _write(root, "20261001T000000Z", _launch("a0000001"))
    cache = tmp_path / "cache" / "history.json"
    cache.parent.mkdir(parents=True, mode=0o700)
    cache.write_text('{"schema_version":1,"files":{"bad":{}}}', encoding="utf-8")
    cache.chmod(0o600)

    launched, _wake, _strong = ns["_load_launch_history"](root, cache, "gpt-5.6-sol")

    assert set(launched) == {"a0000001"}


def test_strong_model_change_reclassifies_cached_history(tmp_path: Path) -> None:
    ns = _namespace()
    root = tmp_path / "evidence"
    _write(root, "20261001T000000Z", _launch("a0000001", "gpt-5.6-sol"))
    cache = tmp_path / "cache" / "history.json"
    ns["_load_launch_history"](root, cache, "glm-m")
    parsed: list[str] = []
    scan = ns["_scan_launch_history_log"]
    ns["_scan_launch_history_log"] = lambda path, epoch, model: (
        parsed.append(str(path)) or scan(path, epoch, model)
    )

    _launched, _wake, strong = ns["_load_launch_history"](root, cache, "gpt-5.6-sol")

    assert parsed == [str(root / "20261001T000000Z" / "actions.log")]
    assert set(strong) == {"a0000001"}
