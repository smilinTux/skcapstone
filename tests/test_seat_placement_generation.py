"""The seat placement manifest is generated, never hand-maintained.

Nothing in the repository wrote ``seat-placement.json`` before this: the live
copy on chiap08 was nine days stale and missing ``atlas`` entirely, while the
dispatcher (`scripts/fleet/skfleet-rotate.py::_load_seat_placement`) fails
closed on a missing or malformed file. This covers the generator that closes
that gap.

Every seat lists exactly ONE host on purpose. The claim fence in CardStore
does not exclude a concurrent second host (``~/.skcapstone`` is per-host local
storage replicated by Syncthing, so ``fcntl.flock`` never reaches across
machines); ``active_host`` in the seat control plane is the only cross-host
exclusion this estate has. Two hosts for one seat would dispatch the same
cards twice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.lifecycle_seats import (
    LIFECYCLE_SEATS,
    generate_seat_placement_manifest,
    write_seat_placement_manifest,
)


def _write_estate(home: Path, rotation_hosts: list[str]) -> None:
    path = home / "config/estate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "sk.estate-authority/v1",
                "operator": "chef",
                "realm": "skworld.io",
                "rotation_hosts": rotation_hosts,
            }
        ),
        encoding="utf-8",
    )


def test_manifest_covers_every_roster_seat(tmp_path: Path) -> None:
    _write_estate(tmp_path, ["chiap08", "chiap01"])
    manifest = generate_seat_placement_manifest(active_host="chiap08", home=tmp_path)
    assert manifest["schema_version"] == 1
    assert set(manifest["seats"]) == LIFECYCLE_SEATS


def test_every_seat_lists_exactly_one_host(tmp_path: Path) -> None:
    _write_estate(tmp_path, ["chiap08", "chiap01"])
    manifest = generate_seat_placement_manifest(active_host="chiap08", home=tmp_path)
    for seat, hosts in manifest["seats"].items():
        assert hosts == ["chiap08"], f"seat {seat} must list exactly one host"


def test_unknown_host_is_rejected(tmp_path: Path) -> None:
    _write_estate(tmp_path, ["chiap08", "chiap01"])
    with pytest.raises(ValueError, match="rotation hosts"):
        generate_seat_placement_manifest(active_host="ghost-host", home=tmp_path)


def test_generator_is_idempotent(tmp_path: Path) -> None:
    _write_estate(tmp_path, ["chiap08", "chiap01"])
    first = generate_seat_placement_manifest(active_host="chiap08", home=tmp_path)
    second = generate_seat_placement_manifest(active_host="chiap08", home=tmp_path)
    assert first == second


def test_write_is_idempotent_on_disk(tmp_path: Path) -> None:
    _write_estate(tmp_path, ["chiap08", "chiap01"])
    target = tmp_path / "coordination/seat-placement.json"
    write_seat_placement_manifest(target, active_host="chiap08", home=tmp_path)
    first_bytes = target.read_bytes()
    write_seat_placement_manifest(target, active_host="chiap08", home=tmp_path)
    second_bytes = target.read_bytes()
    assert first_bytes == second_bytes


def test_write_is_atomic_tmp_plus_replace(tmp_path: Path, monkeypatch) -> None:
    _write_estate(tmp_path, ["chiap08", "chiap01"])
    target = tmp_path / "coordination/seat-placement.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    calls: list[tuple[str, str]] = []
    import os as os_module

    real_replace = os_module.replace

    def spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        # The tmp file lives beside the target, in the same directory.
        assert Path(src).parent == Path(dst).parent
        assert Path(src) != Path(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", spy_replace)
    write_seat_placement_manifest(target, active_host="chiap08", home=tmp_path)

    assert len(calls) == 1
    assert calls[0][1] == str(target)
    # No stray tmp file left behind after a successful write.
    leftovers = [p for p in target.parent.iterdir() if p != target]
    assert leftovers == []


def test_atomic_write_leaves_no_partial_file_on_failure(tmp_path: Path, monkeypatch) -> None:
    _write_estate(tmp_path, ["chiap08", "chiap01"])
    target = tmp_path / "coordination/seat-placement.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    import os as os_module

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os_module, "replace", boom)
    with pytest.raises(OSError):
        write_seat_placement_manifest(target, active_host="chiap08", home=tmp_path)

    assert not target.exists()
    # The failed tmp file must be cleaned up, not left behind.
    assert list(target.parent.iterdir()) == []
