"""The drill guard must not be relocatable by the environment (gap G0).

`os.path.expanduser` prefers the `HOME` environment variable, so a guard
built on it moves when `HOME` moves. Drilled during card `4c32df6f`: under a
rewritten `HOME` the guard computed a different forbidden prefix and ACCEPTED
the real production tree as a drill root. No write happened, but the refusal
that is the entire point of the guard did not fire.

A guard whose definition of "production" is supplied by the caller is not a
guard.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from skcapstone.fleet import drill

PRODUCTION = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".skcapstone" / "fleet"


def test_sovereign_home_ignores_a_rewritten_HOME(tmp_path, monkeypatch) -> None:
    before = drill.sovereign_home()
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    assert drill.sovereign_home() == before, "the forbidden prefix moved with $HOME"


def test_production_is_still_refused_under_a_rewritten_HOME(tmp_path, monkeypatch) -> None:
    """The load-bearing case: this is the probe that did not fire in the drill."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    with pytest.raises(drill.UnsafeDrillRootError):
        drill.resolve_drill_root(PRODUCTION)


def test_production_is_refused_with_HOME_unset(monkeypatch) -> None:
    """Deleting HOME entirely must not disarm the guard either."""
    monkeypatch.delenv("HOME", raising=False)
    with pytest.raises(drill.UnsafeDrillRootError):
        drill.resolve_drill_root(PRODUCTION)


def test_a_genuine_scratch_root_is_still_accepted(tmp_path, monkeypatch) -> None:
    """Positive control: the fix must not refuse everything.

    A guard that refuses every root would pass both tests above while being
    completely useless, so this pins that legitimate drills still work.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    scratch = tmp_path / "scratch-fleet"
    assert drill.resolve_drill_root(scratch) == scratch.resolve()


def test_the_sovereign_home_itself_is_refused_under_a_rewritten_HOME(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    with pytest.raises(drill.UnsafeDrillRootError):
        drill.resolve_drill_root(PRODUCTION.parent)
