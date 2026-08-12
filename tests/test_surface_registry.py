"""Tests for skdashboard.surface_registry."""

from __future__ import annotations

import pytest

from skdashboard.surface_registry import (
    KNOWN_SURFACES,
    SURFACE_PREFIX,
    is_known_surface,
    parse_card_id,
    resolve_card_id,
)


def test_known_surfaces_contains_expected_set() -> None:
    """KNOWN_SURFACES must include all five documented fleet surfaces."""
    assert {"coord", "gtd", "itil", "chat", "security"} <= KNOWN_SURFACES


def test_surface_prefix_mapping() -> None:
    """SURFACE_PREFIX must map each surface to its documented prefix."""
    assert SURFACE_PREFIX == {
        "coord": "",
        "gtd": "gtd-",
        "itil": "",
        "chat": "thr-",
        "security": "sec-",
    }


@pytest.mark.parametrize("surface", ["coord", "gtd", "itil", "chat", "security"])
def test_is_known_surface_true_for_known(surface: str) -> None:
    """Every documented surface is reported known."""
    assert is_known_surface(surface) is True


@pytest.mark.parametrize("surface", ["", "unknown", "GTD", "coord "])
def test_is_known_surface_false_for_unknown(surface: str) -> None:
    """Unrecognized or mis-cased surface names are reported unknown."""
    assert is_known_surface(surface) is False


def test_resolve_card_id_gtd_adds_prefix() -> None:
    """A raw GTD item id gets the gtd- prefix applied."""
    assert resolve_card_id("gtd", "abc123") == "gtd-abc123"


def test_resolve_card_id_chat_adds_prefix() -> None:
    """A raw chat thread id gets the thr- prefix applied."""
    assert resolve_card_id("chat", "x") == "thr-x"


def test_resolve_card_id_security_adds_prefix() -> None:
    """A raw security finding id gets the sec- prefix applied."""
    assert resolve_card_id("security", "42") == "sec-42"


def test_resolve_card_id_coord_passthrough() -> None:
    """Coord tasks use the raw task id unchanged."""
    assert resolve_card_id("coord", "task-99") == "task-99"


def test_resolve_card_id_itil_passthrough() -> None:
    """ITIL record ids already carry their own prefix and pass through unchanged."""
    assert resolve_card_id("itil", "inc-7") == "inc-7"
    assert resolve_card_id("itil", "prb-3") == "prb-3"
    assert resolve_card_id("itil", "chg-1") == "chg-1"


def test_resolve_card_id_unknown_surface_returns_none() -> None:
    """An unrecognized surface resolves to None regardless of item_id."""
    assert resolve_card_id("bogus", "abc") is None


@pytest.mark.parametrize("item_id", ["", "   "])
def test_resolve_card_id_blank_item_id_returns_none(item_id: str) -> None:
    """An empty or whitespace-only item id resolves to None."""
    assert resolve_card_id("gtd", item_id) is None


def test_resolve_card_id_gtd_idempotent_when_already_prefixed() -> None:
    """A gtd item id already carrying gtd- is not double-prefixed."""
    assert resolve_card_id("gtd", "gtd-abc") == "gtd-abc"


def test_resolve_card_id_chat_idempotent_when_already_prefixed() -> None:
    """A chat item id already carrying thr- is not double-prefixed."""
    assert resolve_card_id("chat", "thr-x") == "thr-x"


def test_resolve_card_id_security_idempotent_when_already_prefixed() -> None:
    """A security item id already carrying sec- is not double-prefixed."""
    assert resolve_card_id("security", "sec-42") == "sec-42"


def test_resolve_card_id_strips_whitespace() -> None:
    """Surrounding whitespace on item_id is stripped before prefixing."""
    assert resolve_card_id("gtd", "  abc  ") == "gtd-abc"
    assert resolve_card_id("coord", "  task-1  ") == "task-1"


def test_parse_card_id_gtd_round_trip() -> None:
    """gtd-abc parses back to ("gtd", "abc")."""
    assert parse_card_id("gtd-abc") == ("gtd", "abc")


def test_parse_card_id_chat_round_trip() -> None:
    """thr-x parses back to ("chat", "x")."""
    assert parse_card_id("thr-x") == ("chat", "x")


def test_parse_card_id_security_round_trip() -> None:
    """sec-x parses back to ("security", "x")."""
    assert parse_card_id("sec-x") == ("security", "x")


@pytest.mark.parametrize("card_id", ["inc-1", "prb-1", "chg-1"])
def test_parse_card_id_itil_classifies_and_keeps_full_id(card_id: str) -> None:
    """ITIL-prefixed ids classify as itil and keep the full id as item_id."""
    assert parse_card_id(card_id) == ("itil", card_id)


def test_parse_card_id_unrecognized_defaults_to_coord() -> None:
    """Anything without a known prefix defaults to a coord classification."""
    assert parse_card_id("task-99") == ("coord", "task-99")


def test_resolve_then_parse_round_trip_for_gtd() -> None:
    """resolve_card_id and parse_card_id are inverses for the gtd surface."""
    card_id = resolve_card_id("gtd", "abc123")
    assert card_id is not None
    assert parse_card_id(card_id) == ("gtd", "abc123")
