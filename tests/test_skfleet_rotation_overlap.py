"""Regression tests for standing-seat rotation lock ownership."""

from __future__ import annotations

import ast
import fcntl
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_functions(*names: str):
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = [
        item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name in names
    ]
    namespace = {"os": __import__("os"), "re": __import__("re"), "_SEAT_RE": None}
    seat_re = next(
        item
        for item in tree.body
        if isinstance(item, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_SEAT_RE" for target in item.targets
        )
    )
    exec(
        compile(ast.Module(body=[seat_re, *nodes], type_ignores=[]), str(ROTATE), "exec"),
        namespace,
    )
    return tuple(namespace[name] for name in names)


def _load_lock_path():
    return _load_functions("_rotation_lock_path")[0]


@pytest.mark.parametrize("seat", ["bad/name", "../escape", "_invalid", "seat name"])
def test_invalid_seat_is_rejected_before_lock_path_access(seat: str) -> None:
    """Invalid identity cannot influence a lock path or filesystem open."""
    validate, lock_path = _load_functions("_validated_rotation_seat", "_rotation_lock_path")
    with pytest.raises(SystemExit, match=r"BLOCKED\|SKFLEET_ONLY_SEAT\|invalid seat"):
        lock_path("/must-not-be-used", validate(seat))

    source = ROTATE.read_text(encoding="utf-8")
    assert source.index("ONLY_SEAT=_validated_rotation_seat(") < source.index(
        "lock=open(_rotation_lock_path(HOME,ONLY_SEAT)"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("  SERAPH  ", "seraph"), ("link", "link"), ("mero-2", "mero-2")],
)
def test_validated_seat_normalizes_only_valid_identity(raw: str, expected: str) -> None:
    validate = _load_functions("_validated_rotation_seat")[0]
    assert validate(raw) == expected


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
