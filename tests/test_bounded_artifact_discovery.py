"""Regression tests for bounded SHA256 evidence discovery (card 4cd4dd63)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skcapstone.fleet.bounded_artifact_discovery import (
    DiscoveryBounds,
    discover_card_artifacts,
    find_digests,
)


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def test_find_digests_honors_file_and_time_bounds(tmp_path: Path) -> None:
    """Discovery stops at deterministic file-count and time budgets."""
    root = tmp_path / "evidence" / "work" / "abcd1234"
    target = _write(root / "candidate.patch", b"exact-candidate-bytes")
    for index in range(20):
        _write(root / f"decoy-{index}.bin", f"decoy-{index}".encode())

    missing = "a" * 64
    file_limited = find_digests(
        [missing],
        roots=[root],
        bounds=DiscoveryBounds(max_files=3, max_seconds=30.0),
    )
    assert file_limited.stopped_reason == "max_files"
    assert file_limited.files_examined == 3
    assert file_limited.digests[missing] is None
    assert file_limited.bounded is True

    calls = {"n": 0}

    def advance() -> float:
        # 0.0 start, then 0.6 per subsequent sample so the third check trips 1.5s.
        value = 0.6 * calls["n"]
        calls["n"] += 1
        return value

    timed = find_digests(
        ["f" * 64],
        roots=[root],
        bounds=DiscoveryBounds(max_files=1000, max_seconds=1.5),
        clock=advance,
    )
    assert timed.stopped_reason == "max_seconds"
    assert timed.files_examined >= 1
    assert timed.bounded is True


def test_discovery_never_walks_estate_outside_configured_roots(tmp_path: Path) -> None:
    """Estate-wide trees are invisible unless explicitly configured as roots."""
    home = tmp_path / ".skcapstone"
    work = home / "evidence" / "work" / "383a7834"
    estate = home / "evidence" / "fleet-rotation"
    worktrees = home / "worktrees" / "sklegal"
    hidden_digest = _write(estate / "hidden.patch", b"hidden-estate-bytes")
    _write(worktrees / "also-hidden.patch", b"worktree-bytes")
    visible = _write(work / "candidate.patch", b"visible-candidate")

    result = discover_card_artifacts(
        home,
        {
            "id": "4cd4dd62",
            "description": f"verify sha256 {hidden_digest} and {visible}",
            "acceptance_criteria": [f"candidate evidence sha256 {visible}"],
            "links": {},
            "meta": {"link_source_card": "383a7834"},
        },
        bounds=DiscoveryBounds(max_files=64, max_seconds=5.0),
    )
    assert result.digests[visible] is not None
    assert str(work) in result.digests[visible]
    assert result.digests[hidden_digest] is None
    assert all("fleet-rotation" not in root for root in result.roots_used)
    assert all("worktrees" not in root for root in result.roots_used)
    assert result.stopped_reason == "complete"


def test_explicit_path_is_checked_without_estate_scan(tmp_path: Path) -> None:
    """An explicit card path resolves even when no root walk is needed."""
    path = tmp_path / "exact.json"
    digest = _write(path, b'{"ok":true}')
    poison = tmp_path / "estate"
    for index in range(50):
        _write(poison / f"{index}.bin", f"poison-{index}".encode())

    result = find_digests(
        [digest],
        explicit_paths=[path],
        roots=[poison],
        bounds=DiscoveryBounds(max_files=1, max_seconds=5.0),
    )
    assert result.digests[digest] == str(path.resolve())
    assert result.files_examined == 1
    assert result.stopped_reason == "complete"
