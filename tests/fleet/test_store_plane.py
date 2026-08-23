"""Plane control files (freeze + carve-out manifest) are HUMAN-only.

The autonomous AI operator seat uses the operator role for the ops channel, but
it must never toggle the freeze or write the carve-out manifest. The human keeps
the only card that overrides the AI.
"""

from __future__ import annotations

import pytest

from skcapstone.fleet import store


def _human() -> store.Writer:
    return store.Writer(role="operator", node="node-158", identity="chef")


def _ai_seat() -> store.Writer:
    return store.Writer(role="operator", node="node-41", identity="operator", agent_seat=True)


def test_human_can_freeze(paths) -> None:
    store.set_frozen(paths, True, writer=_human(), reason="drill")
    assert store.is_frozen(paths) is True


def test_ai_seat_cannot_freeze(paths) -> None:
    with pytest.raises(store.OwnershipError):
        store.set_frozen(paths, True, writer=_ai_seat())
    assert store.is_frozen(paths) is False


def test_ai_seat_cannot_unfreeze(paths) -> None:
    store.set_frozen(paths, True, writer=_human())
    with pytest.raises(store.OwnershipError):
        store.set_frozen(paths, False, writer=_ai_seat())
    assert store.is_frozen(paths) is True  # still frozen; the AI cannot lift it


def test_human_can_write_plane_file(paths) -> None:
    out = store.write_plane_file(paths, "_protected", {"protected": ["*/x.py"]}, writer=_human())
    assert out["protected"] == ["*/x.py"]


def test_ai_seat_cannot_write_plane_file(paths) -> None:
    with pytest.raises(store.OwnershipError):
        store.write_plane_file(paths, "_protected", {"protected": []}, writer=_ai_seat())


def test_plane_file_name_must_be_underscore(paths) -> None:
    with pytest.raises(store.OwnershipError):
        store.write_plane_file(paths, "node-41", {}, writer=_human())
