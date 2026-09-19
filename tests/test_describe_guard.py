"""Card titles that are argv fragments must be refused, not stored."""

from __future__ import annotations

import pytest

from skcapstone.describe_guard import DegenerateTitleError, check_title


@pytest.mark.parametrize("title", ["--description", "--desc", "--title", "-t"])
def test_an_option_name_passed_as_a_value_is_refused(title: str) -> None:
    """Every real corruption event on chi was an option name used as a value."""
    with pytest.raises(DegenerateTitleError, match="option name"):
        check_title(title)


@pytest.mark.parametrize("title", ["x", "y", "X", " test ", "foo"])
def test_placeholder_titles_are_refused(title: str) -> None:
    """A tool poked with placeholder values destroyed 13 live cards."""
    with pytest.raises(DegenerateTitleError):
        check_title(title)


def test_none_means_do_not_change_the_title() -> None:
    check_title(None)


def test_empty_string_is_allowed_because_clearing_is_explicit() -> None:
    """The size resolver has a defined fallback for an empty title."""
    check_title("")


@pytest.mark.parametrize(
    "title",
    [
        "[W72-KNEXT][S] Authority contradiction retention audit",
        "[SKDASH-STREAM-1][M] Serve projected worker activity",
        "Fix the thing",
    ],
)
def test_real_titles_pass(title: str) -> None:
    """A guard that refuses legitimate input gets routed around."""
    check_title(title)
