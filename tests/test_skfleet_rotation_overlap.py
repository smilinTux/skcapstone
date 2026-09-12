"""Regression tests for standing-seat rotation lock ownership."""

from __future__ import annotations

import ast
import fcntl
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_lock_path():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_rotation_lock_path"
    )
    namespace = {"os": __import__("os")}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_rotation_lock_path"]


def test_niobe_does_not_collide_with_other_standing_seat_timers(tmp_path: Path) -> None:
    """Each standing seat owns an independent rotation lock."""
    lock_path = _load_lock_path()
    niobe_path = Path(lock_path(tmp_path, "niobe"))
    other_paths = [
        Path(lock_path(tmp_path, seat)) for seat in ("link", "mero", "seraph", "tank", "atlas")
    ]

    assert niobe_path not in other_paths
    assert len(set(other_paths)) == len(other_paths)

    niobe_path.parent.mkdir(parents=True)
    with niobe_path.open("w") as niobe_lock:
        fcntl.flock(niobe_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with niobe_path.open("w") as overlapping_niobe_lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(overlapping_niobe_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in other_paths:
            with path.open("w") as seat_lock:
                fcntl.flock(seat_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_unseated_rotation_retains_the_global_lock(tmp_path: Path) -> None:
    """The generic rotation path keeps its existing single-flight lock."""
    lock_path = _load_lock_path()

    assert Path(lock_path(tmp_path, "")) == tmp_path / ".skcapstone/fleet/rotate.lock"
