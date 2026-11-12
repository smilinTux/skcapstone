"""CI guard: a skmemory bump must move the fleet pin in the same PR."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "chk", Path(__file__).resolve().parents[1] / "scripts/ci/check_skmemory_pin_moves.py"
)
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)

PP = lambda v: f'dependencies = [\n    "skmemory>={v}",\n]\n'  # noqa: E731
FL = lambda v: f"skcoord==0.1.90\nskmemory=={v}\n"  # noqa: E731


def test_floor_bump_without_pin_move_fails():
    e = chk.check(
        {"pyproject.toml": (PP("0.11.68"), PP("0.11.70"))},
        FL("0.11.75"),
        FL("0.11.75"),
        PP("0.11.70"),
    )
    assert e and "did not move" in e[0]


def test_floor_bump_with_pin_move_passes():
    assert not chk.check(
        {"pyproject.toml": (PP("0.11.68"), PP("0.11.76"))},
        FL("0.11.75"),
        FL("0.11.76"),
        PP("0.11.76"),
    )


def test_unrelated_change_passes():
    assert not chk.check(
        {"pyproject.toml": (PP("0.11.68"), PP("0.11.68"))},
        FL("0.11.75"),
        FL("0.11.75"),
        PP("0.11.68"),
    )


def test_floor_above_pin_fails():
    e = chk.check(
        {"pyproject.toml": (PP("0.11.68"), PP("0.11.80"))},
        FL("0.11.75"),
        FL("0.11.76"),
        PP("0.11.80"),
    )
    assert any("exceeds" in x for x in e)


def test_live_repo_is_consistent():
    root = Path(__file__).resolve().parents[1]
    assert not chk.check(
        {}, "", (root / chk.FLEET).read_text(), (root / "pyproject.toml").read_text()
    )
