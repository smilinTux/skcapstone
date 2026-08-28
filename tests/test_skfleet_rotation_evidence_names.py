"""Compatibility tests for host-partitioned fleet rotation evidence."""

from __future__ import annotations

import ast
import collections
import datetime
import glob
import json
import os
import re
import time
from pathlib import Path

import pytest
from skcoord.lifecycle_reassessment import _launch_counts, assess

_SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"
_FUNCTION_NAMES = {
    "_read_launch_history",
    "_rotation_action_logs",
    "_rotation_batch_dir",
    "_rotation_batch_epoch",
    "_shared_launch_attempts",
}
_HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def _load_functions() -> dict[str, object]:
    """Compile the evidence helpers without running the rotation script."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"), filename=str(_SOURCE))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCTION_NAMES
    ]
    namespace: dict[str, object] = {
        "ESC_MODEL": "gpt-test",
        "ROTATION_HOSTS": _HOSTS,
        "_EVIDENCE_STAMP_RE": re.compile(r"^\d{8}T\d{6}Z$"),
        "Path": Path,
        "collections": collections,
        "datetime": datetime,
        "glob": glob,
        "os": os,
        "time": time,
    }
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    exec(compile(module, str(_SOURCE), "exec"), namespace)
    return namespace


def _write_log(directory: Path, *lines: str) -> Path:
    """Create one rotation action log fixture."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "actions.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_five_hosts_have_unique_paths_for_both_evidence_files(tmp_path: Path) -> None:
    helpers = _load_functions()
    stamp = "20260828T162700Z"

    directories = {
        Path(helpers["_rotation_batch_dir"](str(tmp_path), host, stamp)) for host in _HOSTS
    }
    action_paths = {directory / "actions.log" for directory in directories}
    lifecycle_paths = {directory / "lifecycle-reassessment.json" for directory in directories}

    assert len(directories) == 5
    assert len(action_paths) == 5
    assert len(lifecycle_paths) == 5
    assert action_paths.isdisjoint(lifecycle_paths)
    assert {path.parent.name for path in action_paths} == {f"{host}_{stamp}" for host in _HOSTS}


@pytest.mark.parametrize(
    ("host", "stamp"),
    [("unknown", "20260828T162700Z"), ("chiap01", "not-a-stamp")],
)
def test_batch_directory_rejects_unvalidated_identity(
    tmp_path: Path, host: str, stamp: str
) -> None:
    helpers = _load_functions()

    with pytest.raises(ValueError):
        helpers["_rotation_batch_dir"](str(tmp_path), host, stamp)


def test_action_log_reader_returns_legacy_and_new_paths_once(tmp_path: Path) -> None:
    helpers = _load_functions()
    stamp = "20260828T162700Z"
    legacy = _write_log(tmp_path / stamp, "LAUNCHED|legacy|session|legacy-card")
    new_paths = {
        _write_log(
            tmp_path / f"{host}_{stamp}",
            f"LAUNCHED|{host}|session|{host}-card",
        )
        for host in _HOSTS
    }
    _write_log(
        tmp_path / f"chiap01_{stamp}",
        "LAUNCHED|chiap01|session|chiap01-card",
    ).with_name(
        "actions.sync-conflict-copy.log"
    ).write_text("LAUNCHED|chiap01|session|must-not-count\n", encoding="utf-8")

    paths = helpers["_rotation_action_logs"](str(tmp_path))

    assert paths == sorted({str(legacy), *(str(path) for path in new_paths)})


def test_launch_history_preserves_backoff_times_and_counts(tmp_path: Path) -> None:
    helpers = _load_functions()
    legacy_stamp = "20260828T162700Z"
    new_stamp = "20260828T162701Z"
    _write_log(
        tmp_path / legacy_stamp,
        "LAUNCHED|chiap01|session|shared-card|lane=codex|model=ordinary",
    )
    _write_log(
        tmp_path / f"chiap02_{new_stamp}",
        "LAUNCHED|chiap02|session|shared-card|lane=codex|model=gpt-test",
    )

    launched, launched_at, strong_launched_at = helpers["_read_launch_history"](str(tmp_path))

    expected_epoch = helpers["_rotation_batch_epoch"](f"chiap02_{new_stamp}")
    assert launched == {"shared-card": 2}
    assert launched_at == {"shared-card": expected_epoch}
    assert strong_launched_at == {"shared-card": expected_epoch}
    assert helpers["_rotation_batch_epoch"](legacy_stamp) > 0


def test_shared_attempts_and_lifecycle_fold_consume_both_generations(
    tmp_path: Path,
) -> None:
    helpers = _load_functions()
    stamp = "20260828T162700Z"
    _write_log(
        tmp_path / stamp,
        "LAUNCHED|chiap01|session|legacy-only",
        "LAUNCHED|chiap01|session|shared-card",
    )
    _write_log(
        tmp_path / f"chiap02_{stamp}",
        "LAUNCHED|chiap02|session|new-only",
        "LAUNCHED|chiap02|session|shared-card",
    )
    helpers["_ROTATION_EVID"] = str(tmp_path)
    helpers["_LAUNCH_TTL_H"] = 24
    helpers["_shared_launch_cache"] = None

    assert helpers["_shared_launch_attempts"]("legacy-only") == 1
    assert helpers["_shared_launch_attempts"]("new-only") == 1
    assert helpers["_shared_launch_attempts"]("shared-card") == 2
    assert _launch_counts([tmp_path]) == {
        "legacy-only": 1,
        "new-only": 1,
        "shared-card": 2,
    }


def test_full_lifecycle_assessment_preserves_mixed_generation_counts(
    tmp_path: Path,
) -> None:
    cards = tmp_path / "cards"
    card = cards / "mixed-card"
    (card / "events").mkdir(parents=True)
    (card / "core.json").write_text(
        json.dumps(
            {
                "id": "mixed-card",
                "kind": "task",
                "title": "mixed-card",
                "description": "",
                "created_at": "2026-08-28T00:00:00+00:00",
                "dependencies": [],
                "initial_labels": [],
            }
        ),
        encoding="utf-8",
    )
    logs = tmp_path / "logs"
    _write_log(
        logs / "20260828T162700Z",
        "LAUNCHED|chiap01|session|mixed-card",
    )
    _write_log(
        logs / "chiap02_20260828T162701Z",
        "LAUNCHED|chiap02|session|mixed-card",
    )

    report = assess(
        cards,
        [logs],
        now=datetime.datetime(2026, 8, 28, tzinfo=datetime.timezone.utc),
    )

    assert report["counts"]["unclaimable_cards"] == 1
    assert report["classes"]["unclaimable_cards"] == [
        {
            "card_id": "mixed-card",
            "launch_count": 2,
            "reason": "repeated_launch_without_claim",
        }
    ]
    assert "mixed-card" in report["excluded_card_ids"]


def test_digest_and_distribution_watch_remain_non_readers() -> None:
    root = Path(__file__).resolve().parents[1]
    digest = (root / "scripts" / "fleet" / "skworld-digest.py").read_text(encoding="utf-8")
    distribution = (root / "scripts" / "fleet" / "skfleet-distribution-watch.sh").read_text(
        encoding="utf-8"
    )

    for source in (digest, distribution):
        assert "actions.log" not in source
        assert "lifecycle-reassessment.json" not in source
